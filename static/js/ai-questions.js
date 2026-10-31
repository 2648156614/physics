(() => {
    'use strict';
    const root = document.getElementById('ai-authoring');
    if (!root) return;
    const base = '/admin/ai-questions';
    const form = document.getElementById('ai-form');
    const result = document.getElementById('ai-result');
    let selected = root.dataset.draft || null, rows = [], busy = false, timer, newTopic = false;
    const labels = {queued:'等待处理',generating:'正在生成',validating:'正在验算',ready:'可入库',failed:'未通过',needs_input:'需补充条件',unsuitable:'暂不适用',imported:'已入库'};
    function message(text, error = false) {
        const box = document.getElementById('ai-message');
        box.className = `alert ${error ? 'alert-danger' : 'alert-secondary'}`;
        box.textContent = text;
    }
    async function api(path, options = {}) {
        const response = await fetch(base + path, {...options, headers: {'X-AI-CSRF': root.dataset.csrf, ...options.headers}});
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || (response.status === 403 ? '登录或操作校验已失效，请刷新页面' : '请求失败，请稍后重试'));
        return data;
    }
    function node(tag, text, className) {
        const element = document.createElement(tag);
        if (text !== undefined) element.textContent = text;
        if (className) element.className = className;
        return element;
    }
    function select(row, loadInputs = true) {
        newTopic = false;
        selected = row.id;
        result.replaceChildren(node('h5', row.template?.name || labels[row.status]), node('p', row.message));
        if (row.parent_id) result.append(node('p', '由上一版草稿修改，旧版仍保留。', 'small text-muted'));
        if (row.source_image) {
            const details = node('details'); details.append(node('summary', '查看原题截图'));
            const image = node('img'); image.src = `${base}/drafts/${row.id}/image/image`;
            image.alt = '原题截图，仅供教师核对'; image.className = 'ai-diagram mt-2';
            details.append(image); result.append(details);
        }
        if (row.source && loadInputs) {
            for (const [key, value] of Object.entries(row.source)) {
                const input = form.elements.namedItem(key);
                if (input) input.value = value;
            }
        }
        if (row.diagram) {
            const img = node('img');
            img.src = `${base}/drafts/${row.id}/image/diagram`;
            img.alt = '学生示意图'; img.className = 'ai-diagram'; result.append(img);
        }
        if (row.report) {
            const report = row.report;
            result.append(node('p', `穷举 ${report.candidate_count} 组候选，保留 ${report.valid_count} 组不同参数，${report.target_count} 种目标组合。`));
            if (Object.keys(report.rejections).length) {
                result.append(node('p', '已排除：' + Object.entries(report.rejections).map(([reason, count]) => `${reason}（${count}）`).join('；'), 'small text-muted'));
            }
            result.append(node('p', report.teacher_review, 'small text-muted'));
            let index = 0;
            const preview = node('div', undefined, 'ai-preview');
            const answers = node('p', undefined, 'text-muted');
            const show = () => {
                const sample = row.previews[index];
                // Only compiler-whitelisted HTML is returned by the server.
                const update = () => { preview.innerHTML = sample.problem_text; };
                if (window.physicsMath) window.physicsMath.typeset([preview], update).catch(() => { update(); });
                else update();
                answers.textContent = '答案：' + sample.correct_answers.map((answer, i) => `${Number(answer.toPrecision(10))} ${sample.answer_units[i]}`).join('；');
            };
            result.append(preview, answers);
            const next = node('button', '换一组预览', 'btn btn-sm btn-outline-secondary mb-3');
            next.type = 'button'; next.onclick = () => { index = (index + 1) % row.previews.length; show(); };
            result.append(next); show();
            const details = node('details'); details.append(node('summary', '识题、推导与参数依据'));
            for (const key of ['recognized_text','derivation','knowledge_point','answer_dependencies','range_basis']) {
                if (row.analysis?.[key]) details.append(node('p', row.analysis[key], 'small mt-2'));
            }
            result.append(details);
            const editable = JSON.parse(JSON.stringify(row.template));
            for (const key of ['valid_cases','compiled_version','compiled_hash','compiled_signature','strict','variable_units']) delete editable.generation_strategy[key];
            document.getElementById('ai-manual').value = JSON.stringify({template:editable,analysis:row.analysis}, null, 2);
            if (row.status === 'ready') {
                const label = node('label', undefined, 'd-block my-3');
                const check = node('input'); check.type = 'checkbox'; check.className = 'form-check-input me-2';
                label.append(check, document.createTextNode('我已核对题意、物理模型和考点' + (row.diagram ? '，并确认示意图与随机参数一致' : '')));
                const save = node('button', '加入当前题库', 'btn btn-primary'); save.type = 'button'; save.disabled = true;
                check.onchange = () => { save.disabled = !check.checked; };
                save.onclick = async () => {
                    save.disabled = true;
                    try {
                        await api(`/drafts/${row.id}/import`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({artifact_hash:row.artifact_hash,teacher_confirmed:check.checked,diagram_confirmed:check.checked})});
                        message('已加入当前题库'); await refresh(true);
                    } catch (error) { message(error.message,true); save.disabled = !check.checked; }
                };
                result.append(label, save);
            }
        } else if (row.plan) {
            result.append(node('p', row.plan.reason || '', 'text-muted'));
        }
        document.getElementById('ai-generate').textContent = '补充要求后重新生成';
    }
    async function refresh(force = false) {
        clearTimeout(timer);
        try {
            const data = await api('/drafts?paper_id=' + encodeURIComponent(root.dataset.paper));
            const old = rows.find(row => row.id === selected);
            rows = data.drafts;
            const list = document.getElementById('ai-drafts'); list.replaceChildren();
            for (const row of rows) {
                const button = node('button', `${row.template?.name || '题目草稿'} · ${labels[row.status] || row.status}`, 'btn btn-sm btn-outline-secondary');
                button.type = 'button'; button.onclick = () => select(row); list.append(button);
            }
            if (!rows.length) list.append(node('span', '暂无生成记录', 'text-muted'));
            const current = rows.find(row => row.id === selected);
            if (current && (force || !old || current.status !== old.status)) select(current, !old);
            if (selected && !current && !old) {
                const linked = await api('/drafts/' + encodeURIComponent(selected));
                select(linked);
            }
            if (!selected && rows.length && !newTopic) select(rows[0]);
            if (rows.some(row => ['queued','generating','validating'].includes(row.status))) timer = setTimeout(refresh, 2000);
        } catch (error) { message(error.message,true); timer = setTimeout(refresh,10000); }
    }
    async function submit(manual) {
        if (busy) return;
        busy = true;
        try {
            const data = new FormData(form);
            data.set('paper_id',root.dataset.paper);
            if (selected) data.set('parent_id',selected);
            if (manual) data.set('manual',document.getElementById('ai-manual').value);
            const reply = await api('/drafts',{method:'POST',body:data});
            selected = reply.id;
            message('任务已保存，正在后台处理'); await refresh(true);
        } catch (error) { message(error.message,true); }
        finally { busy = false; }
    }
    form.onsubmit = event => { event.preventDefault(); submit(false); };
    document.getElementById('ai-validate').onclick = () => submit(true);
    document.getElementById('ai-new').onclick = () => {
        selected = null; newTopic = true; form.reset(); document.getElementById('ai-manual').value = '';
        result.replaceChildren(node('p','请上传新的题目图片或输入文字。','text-muted'));
        document.getElementById('ai-generate').textContent = '生成模板';
    };
    document.getElementById('ai-save-rules').onclick = async () => {
        try {
            await api('/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rules:document.getElementById('ai-rules').value})});
            message('默认规范已保存，下次生成生效');
        } catch (error) { message(error.message,true); }
    };
    refresh();
})();
