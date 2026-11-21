(() => {
    const dialog = document.querySelector('[data-record-dialog]');
    const recordName = dialog.querySelector('[data-record-target]');
    let pending;

    document.querySelectorAll('[data-record-delete]').forEach((form) => {
        form.addEventListener('submit', (event) => {
            event.preventDefault();
            if (typeof dialog.showModal !== 'function') {
                if (window.confirm('ลบรายการนี้ใช่ไหม? ลบแล้วกู้คืนไม่ได้')) form.submit();
                return;
            }
            pending = { form, button: event.submitter };
            recordName.textContent = form.dataset.recordName;
            dialog.returnValue = '';
            dialog.showModal();
        });
    });

    dialog.querySelector('[data-record-close]').addEventListener('click', () => dialog.close('cancel'));
    dialog.addEventListener('close', () => {
        if (!pending) return;
        const { form, button } = pending;
        pending = undefined;
        if (dialog.returnValue === 'delete') {
            form.submit();
        } else {
            button?.focus();
        }
    });
})();
