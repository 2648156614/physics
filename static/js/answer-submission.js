(function (root) {
    'use strict';
    function failure(message, uncertain) {
        const error = new Error(message);
        error.uncertain = uncertain;
        return error;
    }
    async function read(response) {
        if (response.redirected || response.status === 401) {
            throw failure('登录已失效，请重新登录。当前输入已保留。', false);
        }
        let data;
        try { data = await response.json(); } catch (_) {
            throw failure(response.status >= 500 ? '服务暂时不可用，正在核对答案是否保存。' : '返回结果异常，当前输入已保留，请联系监考教师。', response.status >= 500 || response.ok);
        }
        if (!data || typeof data !== 'object') throw failure('返回结果异常，正在核对提交结果。', true);
        if (!response.ok) {
            if (data.submission_in_progress) throw failure(data.message || '正在核对提交结果。', true);
            if (typeof data.success !== 'boolean') throw failure('服务暂时异常，正在核对提交结果。', true);
            data.success = false;
            data.message = data.message || (response.status === 403 ? '当前无法参加此考试，请联系监考教师核对时间和 SEB 配置。' : '提交服务暂时不可用，当前输入已保留。');
        }
        return data;
    }
    function newId() {
        if (root.crypto && root.crypto.getRandomValues) {
            return Array.from(root.crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
        }
        return Array.from({length: 32}, () => Math.floor(Math.random() * 16).toString(16)).join('');
    }
    async function requestWithTimeout(url, options, fetcher, timeout) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeout);
        try { return await fetcher(url, {...options, signal: controller.signal}); }
        finally { clearTimeout(timer); }
    }
    async function send(url, payload) {
        try {
            return await requestWithTimeout(url, {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
            }, root.fetch.bind(root), 15000);
        } catch (_) { throw failure('网络连接中断或请求超时，正在核对提交结果。', true); }
    }
    async function recover(id, fetcher = root.fetch.bind(root), wait = ms => new Promise(resolve => setTimeout(resolve, ms))) {
        for (let attempt = 0; attempt < 4; attempt++) {
            if (attempt) await wait(700 * attempt);
            try {
                const response = await requestWithTimeout('/api/submissions/' + id, {cache: 'no-store'}, fetcher, 6000);
                const result = await read(response);
                if (response.ok && result.state === 'done' && result.data) return result.data;
            } catch (_) { /* Leave the original submission unresolved; never resend it. */ }
        }
        return null;
    }
    root.ExamSubmission = {read, newId, recover, send};
})(window);
