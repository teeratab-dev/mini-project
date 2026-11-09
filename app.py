from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3

app = Flask(__name__)
app.secret_key = "change-this-secret"

LABELS = {
    'teams':       {'th': 'ทีม',        'icon': 'fa-shield-halved'},
    'players':     {'th': 'นักแข่ง',     'icon': 'fa-users'},
    'gear':        {'th': 'อุปกรณ์',     'icon': 'fa-headset'},
    'tournaments': {'th': 'รายการแข่ง',  'icon': 'fa-trophy'},
    'matches':     {'th': 'ผลการแข่ง',   'icon': 'fa-crosshairs'},
}

@app.context_processor
def inject_labels():
    return {'LABELS': LABELS}

DB_NAME = "esports_manager.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
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
    conn.commit()
    conn.close()

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
    conn.close()
    featured = tournaments[0] if tournaments else None
    return render_template('user_index.html', counts=counts, matches=matches,
                           tournaments=tournaments[1:4], featured=featured)

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
        SELECT p.*, t.name as team_name 
        FROM players p LEFT JOIN teams t ON p.team_id = t.id
    ''').fetchall()
    conn.close()
    return render_template('user_teams.html', teams=teams, players=players)

@app.route('/players/<int:player_id>')
def user_player(player_id):
    conn = get_db_connection()
    player = conn.execute('''
        SELECT p.*, t.name as team_name, t.game as game, t.coach as coach
        FROM players p LEFT JOIN teams t ON p.team_id = t.id WHERE p.id = ?
    ''', (player_id,)).fetchone()
    if player is None:
        conn.close()
        return redirect(url_for('user_teams'))
    gear = conn.execute('SELECT * FROM gear WHERE player_id = ? ORDER BY category', (player_id,)).fetchall()
    teammates = []
    if player['team_id']:
        teammates = conn.execute('SELECT * FROM players WHERE team_id = ? AND id != ?',
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
        items = conn.execute('SELECT p.*, t.name as team_name FROM players p LEFT JOIN teams t ON p.team_id = t.id').fetchall()
    elif category == 'gear':
        items = conn.execute('SELECT g.*, p.nickname as player_name FROM gear g LEFT JOIN players p ON g.player_id = p.id').fetchall()
    elif category == 'tournaments':
        items = conn.execute('SELECT * FROM tournaments').fetchall()
    elif category == 'matches':
        items = conn.execute('SELECT m.*, t.name as tournament_name FROM matches m LEFT JOIN tournaments t ON m.tournament_id = t.id').fetchall()
        
    conn.close()
    return render_template('admin_list.html', category=category, items=items)

@app.route('/admin/<category>/add', methods=['GET', 'POST'])
def admin_add_form(category):
    if category not in ['teams', 'players', 'gear', 'tournaments', 'matches']:
        return redirect(url_for('admin_dashboard'))
        
    conn = get_db_connection()
    
    if request.method == 'POST':
        if category == 'teams':
            conn.execute('INSERT INTO teams (name, game, coach) VALUES (?, ?, ?)',
                         (request.form['name'], request.form['game'], request.form['coach']))
        elif category == 'players':
            team_id = request.form.get('team_id') or None
            conn.execute('INSERT INTO players (nickname, real_name, role, team_id) VALUES (?, ?, ?, ?)',
                         (request.form['nickname'], request.form['real_name'], request.form['role'], team_id))
        elif category == 'gear':
            player_id = request.form.get('player_id') or None
            conn.execute('INSERT INTO gear (item_name, category, brand, player_id) VALUES (?, ?, ?, ?)',
                         (request.form['item_name'], request.form['category'], request.form['brand'], player_id))
        elif category == 'tournaments':
            conn.execute('INSERT INTO tournaments (name, game, prize_pool, status) VALUES (?, ?, ?, ?)',
                         (request.form['name'], request.form['game'], request.form['prize_pool'], request.form['status']))
        elif category == 'matches':
            tournament_id = request.form.get('tournament_id') or None
            conn.execute('INSERT INTO matches (tournament_id, opponent, result, score) VALUES (?, ?, ?, ?)',
                         (tournament_id, request.form['opponent'], request.form['result'], request.form['score']))
            
        conn.commit()
        conn.close()
        flash(f"เพิ่ม{LABELS[category]['th']}เรียบร้อยแล้ว")
        return redirect(url_for('admin_list', category=category))

    extra_data = {}
    if category in ['players', 'gear']:
        extra_data['teams'] = conn.execute('SELECT * FROM teams').fetchall()
    if category == 'gear':
        extra_data['players'] = conn.execute('SELECT * FROM players').fetchall()
    if category == 'matches':
        extra_data['tournaments'] = conn.execute('SELECT * FROM tournaments').fetchall()
        
    conn.close()
    return render_template('admin_form.html', category=category, **extra_data)

@app.route('/admin/<category>/delete/<int:item_id>', methods=['POST'])
def admin_delete(category, item_id):
    conn = get_db_connection()
    if category in ['teams', 'players', 'gear', 'tournaments', 'matches']:
        conn.execute(f'DELETE FROM {category} WHERE id = ?', (item_id,))
        conn.commit()
        flash("ลบรายการเรียบร้อยแล้ว")
    conn.close()
    return redirect(url_for('admin_list', category=category))

if __name__ == '__main__':
    init_db()
    app.run(debug=True)