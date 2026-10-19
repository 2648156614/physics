(function () {
    'use strict';
    const forms = document.querySelectorAll('form[data-roster-preview]');
    if (!forms.length) return;
    const modalElement = document.createElement('div');
    modalElement.className = 'modal fade';
    modalElement.tabIndex = -1;
    modalElement.setAttribute('aria-label', '预览学生名单');
    modalElement.innerHTML = '<div class="modal-dialog modal-lg modal-dialog-scrollable"><div class="modal-content">' +
        '<div class="modal-header"><h2 class="modal-title fs-5">确认学生名单变更</h2><button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="取消"></button></div>' +
        '<div class="modal-body" aria-live="polite"></div><div class="modal-footer">' +
        '<button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">返回修改</button>' +
        '<button type="button" class="btn btn-primary" data-confirm-roster disabled>确认名单并保存</button></div></div></div>';
    document.body.appendChild(modalElement);
    const modal = bootstrap.Modal.getOrCreateInstance(modalElement);
    const body = modalElement.querySelector('.modal-body');
    const confirmButton = modalElement.querySelector('[data-confirm-roster]');
    let active = null;
    let sequence = 0;

    function addText(parent, tag, value, className) {
        const element = document.createElement(tag);
        element.textContent = value;
        if (className) element.className = className;
        parent.appendChild(element);
        return element;
    }

    function render(data) {
        body.replaceChildren();
        addText(body, 'p', data.mode === 'append' ? '补充名单：原学生继续保留，新学生加入考试。' : '替换名单：未出现在新名单中的原学生会被移出，历史题单和答案保留。');
        addText(body, 'p', `新增 ${data.added.length} 人，保留 ${data.retained.length} 人，移出 ${data.removed.length} 人，未匹配 ${data.missing.length} 行。`, 'fw-semibold');
        for (const [key, label] of [['added', '新增学生'], ['retained', '保留学生'], ['removed', '将移出的学生'], ['missing', '未匹配账户'], ['conflicts', '已属于其他批次，需先处理']]) {
            const rows = data[key];
            if (!rows.length) continue;
            const details = document.createElement('details');
            details.className = 'border rounded p-2 mb-2';
            details.open = key === 'removed' || key === 'missing' || key === 'conflicts';
            addText(details, 'summary', `${label}（${rows.length}）`, ['removed', 'conflicts'].includes(key) ? 'text-danger' : '');
            const list = document.createElement('div');
            list.style.maxHeight = '220px';
            list.style.overflowY = 'auto';
            for (const row of rows) {
                addText(list, 'div', `${row.row_no ? `第 ${row.row_no} 行 · ` : ''}${row.username || ''} ${row.name || ''}${row.reason ? ` · ${row.reason}` : ''}`, 'small py-1');
            }
            details.appendChild(list);
            body.appendChild(details);
        }
        if (data.duplicate_count) addText(body, 'p', `已自动合并 ${data.duplicate_count} 条重复匹配。`, 'small text-muted');
        if (data.missing.length) addText(body, 'p', '未匹配的行不会加入考试；确认后按上述名单保存。', 'small text-muted');
        if (!data.can_confirm) addText(body, 'p', '请先处理名单问题，再重新预览。', 'text-danger');
    }

    for (const form of forms) {
        const token = document.createElement('input');
        token.type = 'hidden';
        token.name = 'roster_preview_token';
        form.appendChild(token);
        form.addEventListener('change', function () { token.value = ''; });
        form.addEventListener('submit', async function (event) {
            const file = form.querySelector('[name="student_file"]');
            if (form.elements.assign_type.value !== 'student' || !file.files.length || token.value) return;
            event.preventDefault();
            const thisSequence = ++sequence;
            active = null;
            confirmButton.disabled = true;
            body.textContent = '正在核对名单，请稍候……';
            modal.show();
            try {
                const data = new FormData(form);
                data.set('preview_target', form.dataset.rosterPreview);
                if (form.dataset.examId) data.set('exam_id', form.dataset.examId);
                if (form.dataset.batchId) data.set('batch_id', form.dataset.batchId);
                const response = await fetch('/admin/exams/roster-preview', {method: 'POST', body: data});
                if (response.redirected || response.status === 401) throw new Error('登录已失效，请重新登录后预览。');
                const result = await response.json();
                if (thisSequence !== sequence) return;
                if (!response.ok || !result.success) throw new Error(result.message || '名单预览失败，请稍后重试。');
                render(result);
                active = {form, token, value: result.token, submitter: event.submitter};
                confirmButton.disabled = !result.can_confirm;
            } catch (error) {
                if (thisSequence === sequence) body.textContent = error.message || '网络连接中断，请重试预览。';
            }
        });
    }
    modalElement.addEventListener('hidden.bs.modal', function () { ++sequence; active = null; });
    confirmButton.addEventListener('click', function () {
        if (!active || confirmButton.disabled) return;
        const pending = active;
        confirmButton.disabled = true;
        pending.token.value = pending.value;
        pending.form.requestSubmit(pending.submitter || undefined);
        modal.hide();
    });
})();
