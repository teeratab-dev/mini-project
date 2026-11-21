import http.client
import ipaddress
import socket
import ssl
from collections import defaultdict
from contextlib import ExitStack, closing, contextmanager
from io import BytesIO
from pathlib import Path
from unicodedata import normalize
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from PIL import Image, ImageChops, ImageDraw, ImageOps

from flask import Flask, Response, abort, render_template, request, redirect, url_for, flash, send_from_directory
import sqlite3

app = Flask(__name__)
app.secret_key = "change-this-secret"

LABELS = {
    'teams':       {'th': 'ทีม', 'en': 'Teams', 'en_singular': 'Team',        'icon': 'fa-shield-halved'},
    'players':     {'th': 'นักแข่ง', 'en': 'Players', 'en_singular': 'Player',     'icon': 'fa-users'},
    'gear':        {'th': 'อุปกรณ์', 'en': 'Gaming Gear', 'en_singular': 'Gear',     'icon': 'fa-headset'},
    'tournaments': {'th': 'รายการแข่ง', 'en': 'Tournaments', 'en_singular': 'Tournament',  'icon': 'fa-trophy'},
    'matches':     {'th': 'ผลการแข่ง', 'en': 'Match Results', 'en_singular': 'Match Result',   'icon': 'fa-crosshairs'},
}

@app.context_processor
def inject_labels():
    return {'LABELS': LABELS, 'IMAGE_CATEGORIES': IMAGE_CATEGORIES, 'IMAGE_ACCEPT': IMAGE_ACCEPT}

DB_NAME = Path(__file__).with_name("esports_manager.db")
UPLOAD_DIR = DB_NAME.with_name("uploads")
ITEM_NAME_FIELDS = {'players': 'nickname', 'gear': 'item_name'}
IMAGE_CATEGORIES = tuple(ITEM_NAME_FIELDS)
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_ACCEPT = 'image/jpeg,image/png,image/webp'
IMAGE_EXTENSIONS = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}
MAX_URL_LENGTH = 2048
FETCH_TIMEOUT = 10
MAX_REDIRECTS = 3


def image_content_type(data):
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def remove_white_background(source, original):
    if getattr(source, 'is_animated', False):
        return original
    image = ImageOps.exif_transpose(source).convert('RGBA')
    if image.getchannel('A').getextrema()[0] < 255:
        return original

    image.thumbnail((1600, 1600))
    width, height = image.size
    red, green, blue = image.convert('RGB').split()
    lightness = ImageChops.darker(ImageChops.darker(red, green), blue)
    white = lightness.point([255 if level >= 235 else 0 for level in range(256)])
    corners = ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
    if sum(white.getpixel(point) == 255 for point in corners) < 3:
        return original

    # The padded border joins white pixels reachable from any edge.
    padded = ImageOps.expand(white, border=1, fill=255)
    ImageDraw.floodfill(padded, (0, 0), 128)
    alpha = padded.crop((1, 1, width + 1, height + 1)).point(
        [0 if level == 128 else 255 for level in range(256)]
    )
    if alpha.getextrema() != (0, 255):
        return original
    image.putalpha(alpha)
    output = BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


def process_image(data):
    """Validate downloaded bytes and strip a white background. Raises ValueError with a Thai message."""
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError('รูปต้องมีขนาดไม่เกิน 5 MB')
    if not image_content_type(data):
        raise ValueError('ลิงก์นี้ไม่ใช่ไฟล์รูป JPG, PNG หรือ WebP')
    try:
        with Image.open(BytesIO(data), formats=['JPEG', 'PNG', 'WEBP']) as source:
            if source.width * source.height > 25_000_000:
                raise ValueError('รูปมีความละเอียดเกิน 25 ล้านพิกเซล')
            source.load()
            return remove_white_background(source, data)
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError('เปิดรูปไม่ได้ ไฟล์ในลิงก์นี้ไม่สมบูรณ์') from error


class PinnedHTTPConnection(http.client.HTTPConnection):
    """Connects to an already-validated IP so the host cannot be re-resolved to a private address."""
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self.ip = ip

    def connect(self):
        self.sock = socket.create_connection((self.ip, self.port), self.timeout)


def tls_context():
    # certifi's CA bundle is more reliable than the Windows store; fall back to the system store without it.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout, context=tls_context())
        self.ip = ip

    def connect(self):
        sock = socket.create_connection((self.ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def public_address(host, port):
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as error:
        raise ValueError('ไม่พบเว็บไซต์ของลิงก์นี้') from error
    addresses = [info[4][0].split('%')[0] for info in infos]
    if not addresses or not all(ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError('ลิงก์นี้ชี้ไปยังเครือข่ายภายใน ใช้ไม่ได้')
    return addresses[0]


def fetch_image_url(url):
    """Download an image from a public http(s) URL (size, time and redirect limited)."""
    for _ in range(MAX_REDIRECTS + 1):
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as error:
            raise ValueError('รูปแบบลิงก์ไม่ถูกต้อง') from error
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise ValueError('ลิงก์ต้องขึ้นต้นด้วย http:// หรือ https://')
        port = port or (443 if parts.scheme == 'https' else 80)
        connection_class = PinnedHTTPSConnection if parts.scheme == 'https' else PinnedHTTPConnection
        connection = connection_class(parts.hostname, port, public_address(parts.hostname, port), FETCH_TIMEOUT)
        try:
            target = (parts.path or '/') + (f'?{parts.query}' if parts.query else '')
            connection.request('GET', target, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; ImageFetcher/1.0)',
                'Accept': 'image/jpeg,image/png,image/webp,*/*;q=0.5',
                'Accept-Encoding': 'identity',
            })
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308) and response.getheader('Location'):
                url = urljoin(url, response.getheader('Location'))
                continue
            if response.status != 200:
                raise ValueError(f'ดาวน์โหลดรูปจากลิงก์ไม่ได้ (เว็บตอบกลับ {response.status})')
            declared = response.getheader('Content-Length')
            if declared and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
                raise ValueError('รูปต้องมีขนาดไม่เกิน 5 MB')
            return response.read(MAX_IMAGE_BYTES + 1)
        except (OSError, http.client.HTTPException) as error:
            raise ValueError('เปิดลิงก์นี้ไม่ได้ ตรวจสอบลิงก์หรืออินเทอร์เน็ตแล้วลองใหม่') from error
        finally:
            connection.close()
    raise ValueError('ลิงก์นี้ส่งต่อไปหลายทอดเกินไป')


def read_image():
    """Return (url, processed_bytes) for the submitted image_url field, or None when it is empty."""
    url = request.form.get('image_url', '').strip()
    if not url:
        return None
    if len(url) > MAX_URL_LENGTH:
        raise ValueError('ลิงก์ยาวเกินไป')
    return url, process_image(fetch_image_url(url))


def discard_image(category, filename):
    if not isinstance(filename, str) or Path(filename).name != filename:
        return
    try:
        (UPLOAD_DIR / category / filename).unlink(missing_ok=True)
    except OSError:
        app.logger.warning('Could not remove unused image %s/%s', category, filename, exc_info=True)


@contextmanager
def save_image(category, data):
    if data is None:
        yield None
        return

    extension = IMAGE_EXTENSIONS.get(image_content_type(data))
    if extension is None:
        raise ValueError('Unsupported stored image format')
    filename = f'{uuid4().hex}{extension}'
    try:
        (UPLOAD_DIR / category).mkdir(parents=True, exist_ok=True)
        (UPLOAD_DIR / category / filename).write_bytes(data)
        yield filename
    except BaseException:
        discard_image(category, filename)
        raise


def name_key(name):
    return ' '.join(normalize('NFKC', name).casefold().split())


def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with ExitStack() as files, closing(get_db_connection()) as conn, conn:
        cursor = conn.cursor()
        cursor.execute('BEGIN IMMEDIATE')
    
        cursor.execute('''CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, game TEXT NOT NULL, coach TEXT
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT, nickname TEXT NOT NULL, real_name TEXT, role TEXT NOT NULL, team_id INTEGER,
            FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE SET NULL
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS gear (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_name TEXT NOT NULL, category TEXT NOT NULL, brand TEXT NOT NULL, player_id INTEGER,
            FOREIGN KEY(player_id) REFERENCES players(id) ON DELETE SET NULL
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS tournaments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, game TEXT NOT NULL, prize_pool REAL DEFAULT 0, status TEXT DEFAULT 'Upcoming'
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tournament_id INTEGER, opponent TEXT NOT NULL, result TEXT NOT NULL, score TEXT NOT NULL,
            FOREIGN KEY(tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
        )''')
        if not any(column['name'] == 'season' for column in conn.execute('PRAGMA table_info(teams)')):
            conn.execute('ALTER TABLE teams ADD COLUMN season INTEGER')
        for table in IMAGE_CATEGORIES:
            columns = {column['name'] for column in conn.execute(f'PRAGMA table_info({table})')}
            if 'image' not in columns:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN image TEXT')
            # `image` holds the source link (or a legacy filename); `image_data` holds the processed picture.
            if 'image_data' not in columns:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN image_data BLOB')
            # Older versions stored the image bytes in the `image` column.
            for item in conn.execute(f"SELECT id, image FROM {table} WHERE typeof(image) = 'blob'"):
                filename = files.enter_context(save_image(table, item['image']))
                conn.execute(f'UPDATE {table} SET image = ? WHERE id = ?', (filename, item['id']))
        conn.execute("""CREATE TABLE IF NOT EXISTS gear_players (
            gear_id INTEGER NOT NULL,
            player_id INTEGER NOT NULL,
            PRIMARY KEY (gear_id, player_id),
            FOREIGN KEY (gear_id) REFERENCES gear(id) ON DELETE CASCADE,
            FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
        )""")
        conn.execute('CREATE INDEX IF NOT EXISTS gear_players_player ON gear_players(player_id)')
        gear_models = {}
        rows = conn.execute('SELECT id, item_name, player_id, image FROM gear ORDER BY image IS NULL, id').fetchall()
        for row in rows:
            item_id = row['id']
            model_id = gear_models.setdefault(name_key(row['item_name']), item_id)
            if row['player_id'] is not None:
                conn.execute("""INSERT OR IGNORE INTO gear_players (gear_id, player_id)
                    SELECT ?, id FROM players WHERE id = ?""", (model_id, row['player_id']))
            if model_id != item_id:
                conn.execute("""INSERT OR IGNORE INTO gear_players (gear_id, player_id)
                    SELECT ?, player_id FROM gear_players WHERE gear_id = ?""", (model_id, item_id))
                conn.execute('DELETE FROM gear_players WHERE gear_id = ?', (item_id,))
                conn.execute('DELETE FROM gear WHERE id = ?', (item_id,))
            elif row['player_id'] is not None:
                conn.execute('UPDATE gear SET player_id = NULL WHERE id = ?', (item_id,))



init_db()


@app.route('/admin/image-preview', methods=['POST'])
def admin_image_preview():
    try:
        image = read_image()
        if image is None:
            raise ValueError('กรุณาวางลิงก์รูป')
    except ValueError as error:
        return Response(str(error), status=400, mimetype='text/plain')
    image = image[1]
    return Response(image, mimetype=image_content_type(image),
                    headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})


@app.route('/images/<category>/<int:item_id>')
def item_image(category, item_id):
    if category not in IMAGE_CATEGORIES:
        abort(404)
    conn = get_db_connection()
    item = conn.execute(f'SELECT image, image_data FROM {category} WHERE id = ?', (item_id,)).fetchone()
    conn.close()
    if item is None or item['image'] is None:
        abort(404)
    if item['image_data'] is not None:
        data = bytes(item['image_data'])
        return Response(data, mimetype=image_content_type(data) or 'application/octet-stream',
                        headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-cache'})
    if item['image'].startswith(('http://', 'https://')):
        return redirect(item['image'])  # hosted on the image site; pages normally link there directly
    # Images added by older versions still live in the uploads folder.
    response = send_from_directory(UPLOAD_DIR / category, item['image'], max_age=0, conditional=False)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-cache'
    return response


@app.route('/admin/<category>/<int:item_id>/image', methods=['GET', 'POST'])
def admin_item_image(category, item_id):
    if category not in IMAGE_CATEGORIES:
        abort(404)
    name_column = ITEM_NAME_FIELDS[category]
    conn = get_db_connection()
    item = conn.execute(
        f'SELECT id, {name_column} AS name, image IS NOT NULL AS has_image, '
        f"CASE WHEN image LIKE 'http%' AND image_data IS NULL THEN image END AS image_src, "
        f"CASE WHEN image LIKE 'http%' THEN image END AS image_url "
        f'FROM {category} WHERE id = ?', (item_id,)
    ).fetchone()
    conn.close()
    if item is None:
        abort(404)

    if request.method == 'POST':
        delete_image = request.form.get('image_action') == 'delete'
        try:
            image = None if delete_image else read_image()
            if image is None and not delete_image:
                raise ValueError('กรุณาวางลิงก์รูป')
        except ValueError as error:
            flash(str(error))
            return render_template('admin_image.html', category=category, item=item), 400

        image_url, image_data = image or (None, None)
        with closing(get_db_connection()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute(f'SELECT image FROM {category} WHERE id = ?', (item_id,)).fetchone()
            if previous is None:
                abort(404)
            conn.execute(f'UPDATE {category} SET image = ?, image_data = ? WHERE id = ?',
                         (image_url, image_data, item_id))
        discard_image(category, previous['image'])
        if delete_image and request.accept_mimetypes.best == 'application/json':
            return '', 204
        flash('ลบรูปเรียบร้อยแล้ว' if delete_image else 'บันทึกรูปเรียบร้อยแล้ว')
        return redirect(url_for('admin_list', category=category))

    return render_template('admin_image.html', category=category, item=item)


@app.route('/')
def user_index():
    conn = get_db_connection()
    counts = {
        'teams': conn.execute('SELECT COUNT(*) FROM teams').fetchone()[0],
        'players': conn.execute('SELECT COUNT(*) FROM players').fetchone()[0],
        'tournaments': conn.execute('SELECT COUNT(*) FROM tournaments').fetchone()[0],
        'matches': conn.execute('SELECT COUNT(*) FROM matches').fetchone()[0],
        'wins': conn.execute("SELECT COUNT(*) FROM matches WHERE result='WIN'").fetchone()[0],
    }
    matches = conn.execute('''
        SELECT m.*, t.name as tournament_name
        FROM matches m LEFT JOIN tournaments t ON m.tournament_id = t.id
        ORDER BY m.id DESC LIMIT 6
    ''').fetchall()
    tournaments = conn.execute('''
        SELECT * FROM tournaments
        ORDER BY CASE status WHEN 'Ongoing' THEN 0 WHEN 'Upcoming' THEN 1 ELSE 2 END, prize_pool DESC
    ''').fetchall()
    featured_teams = conn.execute("SELECT id, name, season FROM teams WHERE season = ? AND game = ? ORDER BY name", (2026, "PUBG: BATTLEGROUNDS")).fetchall()
    conn.close()
    featured = tournaments[0] if tournaments else None
    return render_template('user_index.html', counts=counts, matches=matches,
                           tournaments=tournaments[1:4], featured=featured, featured_teams=featured_teams)

@app.route('/tournaments')
def user_tournaments():
    conn = get_db_connection()
    tournaments = [dict(t) for t in conn.execute('''
        SELECT * FROM tournaments
        ORDER BY CASE status WHEN 'Ongoing' THEN 0 WHEN 'Upcoming' THEN 1 ELSE 2 END, prize_pool DESC
    ''').fetchall()]
    matches = [dict(m) for m in conn.execute('SELECT * FROM matches ORDER BY id DESC').fetchall()]
    conn.close()
    for t in tournaments:
        t['matches'] = [m for m in matches if m['tournament_id'] == t['id']]
        t['wins'] = sum(1 for m in t['matches'] if m['result'] == 'WIN')
        t['losses'] = len(t['matches']) - t['wins']
    return render_template('user_tournaments.html', tournaments=tournaments)

@app.route('/teams')
def user_teams():
    conn = get_db_connection()
    teams = conn.execute('SELECT * FROM teams').fetchall()
    players = conn.execute('''
        SELECT p.id, p.nickname, p.real_name, p.role, p.team_id, p.image IS NOT NULL AS has_image,
               CASE WHEN p.image LIKE 'http%' AND p.image_data IS NULL THEN p.image END AS image_src
        FROM players p JOIN teams t ON p.team_id = t.id
        ORDER BY p.nickname COLLATE NOCASE, p.id
    ''').fetchall()
    gear = conn.execute('''
        SELECT g.id, gp.player_id, g.item_name, g.category, g.brand, g.image IS NOT NULL AS has_image, CASE WHEN g.image LIKE 'http%' AND g.image_data IS NULL THEN g.image END AS image_src
        FROM gear g JOIN gear_players gp ON gp.gear_id = g.id JOIN players p ON gp.player_id = p.id JOIN teams t ON p.team_id = t.id
        ORDER BY g.category, g.item_name COLLATE NOCASE, g.id
    ''').fetchall()
    conn.close()

    players_by_team = defaultdict(list)
    for player in players:
        players_by_team[player['team_id']].append(player)
    gear_by_player = defaultdict(list)
    for item in gear:
        gear_by_player[item['player_id']].append(item)

    return render_template('user_teams.html', teams=teams,
                           players_by_team=players_by_team, gear_by_player=gear_by_player)

@app.route('/players/<int:player_id>')
def user_player(player_id):
    conn = get_db_connection()
    player = conn.execute('''
        SELECT p.id, p.nickname, p.real_name, p.role, p.team_id, p.image IS NOT NULL AS has_image,
               CASE WHEN p.image LIKE 'http%' AND p.image_data IS NULL THEN p.image END AS image_src,
               t.name as team_name, t.game as game, t.coach as coach
        FROM players p LEFT JOIN teams t ON p.team_id = t.id WHERE p.id = ?
    ''', (player_id,)).fetchone()
    if player is None:
        conn.close()
        return redirect(url_for('user_teams'))
    gear = conn.execute("SELECT g.id, g.item_name, g.category, g.brand, g.image IS NOT NULL AS has_image, CASE WHEN g.image LIKE 'http%' AND g.image_data IS NULL THEN g.image END AS image_src FROM gear g JOIN gear_players gp ON gp.gear_id = g.id WHERE gp.player_id = ? ORDER BY g.category", (player_id,)).fetchall()
    teammates = []
    if player['team_id']:
        teammates = conn.execute('SELECT id, nickname, role FROM players WHERE team_id = ? AND id != ?',
                                 (player['team_id'], player_id)).fetchall()
    conn.close()
    return render_template('user_player.html', player=player, gear=gear, teammates=teammates)

@app.route('/admin')
def admin_dashboard():
    conn = get_db_connection()
    counts = {
        'teams': conn.execute('SELECT COUNT(*) FROM teams').fetchone()[0],
        'players': conn.execute('SELECT COUNT(*) FROM players').fetchone()[0],
        'gear': conn.execute('SELECT COUNT(*) FROM gear').fetchone()[0],
        'tournaments': conn.execute('SELECT COUNT(*) FROM tournaments').fetchone()[0],
        'matches': conn.execute('SELECT COUNT(*) FROM matches').fetchone()[0]
    }
    conn.close()
    return render_template('admin_dashboard.html', counts=counts)

@app.route('/admin/<category>')
def admin_list(category):
    if category not in ['teams', 'players', 'gear', 'tournaments', 'matches']:
        return redirect(url_for('admin_dashboard'))
    
    conn = get_db_connection()
    items = []
    
    if category == 'teams':
        items = conn.execute('SELECT * FROM teams').fetchall()
    elif category == 'players':
        items = conn.execute("SELECT p.id, p.nickname, p.real_name, p.role, p.team_id, p.image IS NOT NULL AS has_image, CASE WHEN p.image LIKE 'http%' THEN p.image END AS image_url, CASE WHEN p.image LIKE 'http%' AND p.image_data IS NULL THEN p.image END AS image_src, t.name as team_name FROM players p LEFT JOIN teams t ON p.team_id = t.id").fetchall()
    elif category == 'gear':
        items = [dict(row) for row in conn.execute(
            "SELECT id, item_name, category, brand, image IS NOT NULL AS has_image, "
            "CASE WHEN image LIKE 'http%' THEN image END AS image_url, CASE WHEN image LIKE 'http%' AND image_data IS NULL THEN image END AS image_src FROM gear ORDER BY item_name COLLATE NOCASE")]
        users = defaultdict(list)
        for row in conn.execute("""SELECT gp.gear_id, p.nickname FROM gear_players gp
            JOIN players p ON p.id = gp.player_id ORDER BY p.nickname COLLATE NOCASE"""):
            users[row['gear_id']].append(row['nickname'])
        for item in items:
            item['player_names'] = users[item['id']]
    elif category == 'tournaments':
        items = conn.execute('SELECT * FROM tournaments').fetchall()
    elif category == 'matches':
        items = conn.execute('SELECT m.*, t.name as tournament_name FROM matches m LEFT JOIN tournaments t ON m.tournament_id = t.id').fetchall()
        
    conn.close()
    return render_template('admin_list.html', category=category, items=items)

def assign_gear_players(conn, item_id, player_ids):
    conn.executemany("""INSERT OR IGNORE INTO gear_players (gear_id, player_id)
        SELECT ?, id FROM players WHERE id = ?""",
        [(item_id, player_id) for player_id in player_ids if player_id])


@app.route('/admin/gear/<int:item_id>/players', methods=['GET', 'POST'])
def admin_gear_players(item_id):
    with closing(get_db_connection()) as conn, conn:
        if request.method == 'POST':
            conn.execute('BEGIN IMMEDIATE')
        item = conn.execute('SELECT id, item_name FROM gear WHERE id = ?', (item_id,)).fetchone()
        if item is None:
            abort(404)
        if request.method == 'POST':
            conn.execute('DELETE FROM gear_players WHERE gear_id = ?', (item_id,))
            assign_gear_players(conn, item_id, request.form.getlist('player_id'))
        else:
            players = conn.execute("""SELECT p.id, p.nickname, t.name AS team_name,
                gp.player_id IS NOT NULL AS selected FROM players p
                LEFT JOIN teams t ON p.team_id = t.id
                LEFT JOIN gear_players gp ON gp.player_id = p.id AND gp.gear_id = ?
                ORDER BY t.name, p.nickname COLLATE NOCASE""", (item_id,)).fetchall()
            return render_template('admin_gear_players.html', item=item, players=players)
    flash('บันทึกผู้ใช้งานเรียบร้อยแล้ว')
    return redirect(url_for('admin_list', category='gear'))


@app.route('/admin/<category>/add', methods=['GET', 'POST'])
def admin_add_form(category):
    if category not in ['teams', 'players', 'gear', 'tournaments', 'matches']:
        return redirect(url_for('admin_dashboard'))
        
    form_error = None
    if request.method == 'POST':
        try:
            image = read_image() if category in IMAGE_CATEGORIES else None
            image_url, image_data = image or (None, None)
            with closing(get_db_connection()) as conn, conn:
                if category in ITEM_NAME_FIELDS:
                    field = ITEM_NAME_FIELDS[category]
                    name = request.form[field].strip()
                    key = name_key(name)
                    if not key:
                        raise ValueError('กรุณากรอกชื่อ')
                    # Lock the duplicate check and insert together, including records created by older versions.
                    conn.execute('BEGIN IMMEDIATE')
                    names = conn.execute(f'SELECT {field} FROM {category}')
                    if any(name_key(row[field]) == key for row in names):
                        label = LABELS[category]['th']
                        raise ValueError(f'มี{label}ชื่อ “{name}” อยู่แล้ว กรุณาใช้ชื่ออื่น')
                if category == 'teams':
                    conn.execute('INSERT INTO teams (name, game, coach) VALUES (?, ?, ?)',
                                 (request.form['name'], request.form['game'], request.form['coach']))
                elif category == 'players':
                    team_id = request.form.get('team_id') or None
                    conn.execute('INSERT INTO players (nickname, real_name, role, team_id, image, image_data) VALUES (?, ?, ?, ?, ?, ?)',
                                 (name, request.form['real_name'], request.form['role'], team_id, image_url, image_data))
                elif category == 'gear':
                    item = conn.execute('INSERT INTO gear (item_name, category, brand, image, image_data) VALUES (?, ?, ?, ?, ?)',
                                        (name, request.form['category'], request.form['brand'], image_url, image_data))
                    assign_gear_players(conn, item.lastrowid, request.form.getlist('player_id'))
                elif category == 'tournaments':
                    conn.execute('INSERT INTO tournaments (name, game, prize_pool, status) VALUES (?, ?, ?, ?)',
                                 (request.form['name'], request.form['game'], request.form['prize_pool'], request.form['status']))
                elif category == 'matches':
                    tournament_id = request.form.get('tournament_id') or None
                    conn.execute('INSERT INTO matches (tournament_id, opponent, result, score) VALUES (?, ?, ?, ?)',
                                 (tournament_id, request.form['opponent'], request.form['result'], request.form['score']))
        except ValueError as error:
            form_error = str(error)
        else:
            flash(f"เพิ่ม{LABELS[category]['th']}เรียบร้อยแล้ว")
            return redirect(url_for('admin_list', category=category))

    conn = get_db_connection()
    extra_data = {}
    if category in ['players', 'gear']:
        extra_data['teams'] = conn.execute('SELECT * FROM teams').fetchall()
    if category == 'gear':
        extra_data['players'] = conn.execute('SELECT id, nickname FROM players').fetchall()
    if category == 'matches':
        extra_data['tournaments'] = conn.execute('SELECT * FROM tournaments').fetchall()
        
    conn.close()
    return render_template('admin_form.html', category=category, form_error=form_error, **extra_data), 400 if form_error else 200

@app.route('/admin/<category>/delete/<int:item_id>', methods=['POST'])
def admin_delete(category, item_id):
    if category in LABELS:
        previous = None
        with closing(get_db_connection()) as conn, conn:
            if category in IMAGE_CATEGORIES:
                conn.execute('BEGIN IMMEDIATE')
                previous = conn.execute(f'SELECT image FROM {category} WHERE id = ?', (item_id,)).fetchone()
            if category == 'gear':
                conn.execute('DELETE FROM gear_players WHERE gear_id = ?', (item_id,))
            elif category == 'players':
                conn.execute('DELETE FROM gear_players WHERE player_id = ?', (item_id,))
            conn.execute(f'DELETE FROM {category} WHERE id = ?', (item_id,))
        if previous is not None:
            discard_image(category, previous['image'])
        flash("ลบรายการเรียบร้อยแล้ว")
    return redirect(url_for('admin_list', category=category))

if __name__ == '__main__':
    app.run(host="127.0.0.1", port=5000)