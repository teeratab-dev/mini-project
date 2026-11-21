const picker = document.querySelector('[data-image-picker]');
const input = picker.querySelector('[name="image_url"]');
const preview = picker.querySelector('[data-image-preview]');
const placeholder = picker.querySelector('[data-image-placeholder]');
const status = picker.querySelector('[role="status"]');
let savedUrl = picker.dataset.savedUrl || '';
let currentImage = preview.getAttribute('src');
let request;
let timer;
let previewUrl;

function showImage(src) {
    if (previewUrl) {
        URL.revokeObjectURL(previewUrl);
        previewUrl = undefined;
    }
    if (src) {
        preview.src = src;
    } else {
        preview.removeAttribute('src');
    }
    preview.hidden = !src;
    placeholder.hidden = Boolean(src);
}

function isWebUrl(value) {
    try {
        return ['http:', 'https:'].includes(new URL(value).protocol);
    } catch {
        return false;
    }
}

async function updatePreview() {
    clearTimeout(timer);
    request?.abort();
    input.setCustomValidity('');
    status.textContent = '';

    const value = input.value.trim();
    // Empty or unchanged: show the image that is already saved.
    if (!value || value === savedUrl) {
        showImage(currentImage);
        return;
    }
    if (!isWebUrl(value)) {
        const message = 'ลิงก์ต้องขึ้นต้นด้วย http:// หรือ https://';
        input.setCustomValidity(message);
        status.textContent = message;
        showImage(currentImage);
        return;
    }

    request = new AbortController();
    const { signal } = request;
    const form = new FormData();
    form.append('image_url', value);
    status.textContent = 'กำลังโหลดตัวอย่างรูป…';

    try {
        const response = await fetch(picker.dataset.previewUrl, { method: 'POST', body: form, signal });
        if (response.status === 400) {
            const message = await response.text();
            if (signal.aborted) return;
            input.setCustomValidity(message);
            status.textContent = message;
            showImage(currentImage);
            return;
        }
        if (!response.ok) throw new Error('Preview request failed');

        const image = await response.blob();
        if (signal.aborted) return;
        const url = URL.createObjectURL(image);
        showImage(url);
        previewUrl = url;
        status.textContent = '';
    } catch {
        if (!signal.aborted) status.textContent = 'โหลดตัวอย่างไม่ได้ ลองตรวจลิงก์อีกครั้ง หรือกดบันทึกได้เลย';
    }
}

input.addEventListener('input', () => {
    input.setCustomValidity('');
    clearTimeout(timer);
    timer = setTimeout(updatePreview, 600);
});
input.addEventListener('change', updatePreview);

const dialog = picker.closest('[data-image-dialog]');
const editForm = picker.closest('form');
const deleteButton = picker.querySelector('[data-image-delete]');
const deleteForm = deleteButton?.form;
let opener;

if (dialog && typeof dialog.showModal === 'function') {
    const itemName = dialog.querySelector('[data-image-item-name]');

    document.querySelectorAll('[data-image-edit]').forEach((link) => {
        link.addEventListener('click', (event) => {
            if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
            event.preventDefault();
            opener = link;
            editForm.action = link.href;
            deleteForm.action = link.href;
            itemName.textContent = link.dataset.itemName;
            currentImage = link.dataset.currentImage;
            savedUrl = link.dataset.currentUrl || '';
            deleteButton.hidden = !currentImage;
            editForm.reset();
            input.value = savedUrl;
            updatePreview();
            dialog.showModal();
            input.focus();
            input.select();
        });
    });

    dialog.querySelectorAll('[data-image-close]').forEach((button) => {
        button.addEventListener('click', () => dialog.close());
    });

    dialog.addEventListener('close', () => {
        editForm.reset();
        currentImage = undefined;
        savedUrl = '';
        updatePreview();
        opener?.focus();
    });
}

if (deleteForm) {
    const saveButton = editForm.querySelector('[type="submit"]');
    deleteForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (deleteButton.disabled) return;
        const rowLink = opener;
        const isActive = () => !dialog || (dialog.open && opener === rowLink);
        deleteButton.disabled = true;
        saveButton.disabled = true;
        try {
            const response = await fetch(deleteForm.action, {
                method: 'POST',
                headers: { Accept: 'application/json' },
                body: new URLSearchParams({ [deleteButton.name]: deleteButton.value }),
            });
            if (response.status !== 204) throw new Error('Image deletion failed');
            if (rowLink) {
                delete rowLink.dataset.currentImage;
                delete rowLink.dataset.currentUrl;
                rowLink.querySelector('[data-image-edit-label]').textContent = 'ใส่รูป';
                rowLink.closest('tr').querySelector('[data-image-thumbnail]')?.remove();
            }
            if (isActive()) {
                currentImage = undefined;
                savedUrl = '';
                input.value = '';
                updatePreview();
                input.focus();
                deleteButton.hidden = true;
            }
        } catch {
            if (isActive()) status.textContent = 'ลบรูปไม่สำเร็จ กรุณาลองอีกครั้ง';
        } finally {
            deleteButton.disabled = false;
            saveButton.disabled = false;
        }
    });
}

// Re-rendered form (e.g. after a validation error) already contains a link: show its preview.
if (input.value.trim()) updatePreview();
