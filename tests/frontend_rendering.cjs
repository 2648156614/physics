// Run with Node.js, Playwright and Python/Jinja2 available.
// No application database or external network is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const problemTemplate = fs.readFileSync(path.join(root, 'templates/problem_ajax.html'), 'utf8');
const updateProblemText = problemTemplate.slice(
    problemTemplate.indexOf('async function updateProblemText('),
    problemTemplate.indexOf('// 更新题目参数和正确答案')
);
const renderPython = `
import json, sys
from types import SimpleNamespace
from urllib.parse import urlencode
from jinja2 import Environment, FileSystemLoader
data = json.load(sys.stdin)
env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
def url_for(endpoint, **kwargs):
    if endpoint != 'static':
        return '/mock/' + endpoint
    filename = kwargs.pop('filename')
    return '/static/' + filename + ('?' + urlencode(kwargs) if kwargs else '')
env.globals.update(url_for=url_for, get_flashed_messages=lambda **kwargs: [])
template = env.from_string('{% extends "base.html" %}{% block content %}{{ body|safe }}{% endblock %}{% block scripts %}{{ scripts|safe }}{% endblock %}')
print(template.render(**data, session={'user_id': 1, 'username': 'admin' if data['is_admin'] else 'student'}, request=SimpleNamespace(path='/admin' if data['is_admin'] else '/problem_ajax/1', args={}, endpoint='admin_dashboard' if data['is_admin'] else 'problem_ajax'), avatar_filename='default.svg', available_avatars=[]))
`;

function render(admin, failEngine = false) {
    const scripts = (admin ? `
        <script src="/static/vendor/chartjs-4.5.1/chart.umd.js"></script>
        <script src="/static/vendor/echarts-5.5.1/echarts.min.js"></script>
        <script src="/static/vendor/gsap-3.12.5/gsap.min.js"></script>
        <script src="/static/vendor/gsap-3.12.5/ScrollTrigger.min.js"></script>
    ` : '') + `<script>
        window.layoutChecks = [];
        function updateSpotlightLayout() {
            window.layoutChecks.push(!!document.querySelector('#problem-content mjx-container'));
        }
        ${updateProblemText}
        ${failEngine ? 'window.engineFailure = window.physicsMath.ready.then(() => false, () => true);' : `
        window.earlyRender = window.physicsMath.typeset([document.getElementById('problem-content')], () => {
            document.getElementById('problem-content').textContent = '$\\\\frac{1}{2} + \\\\cancel{x}$';
        });
        `}
    </script>`;
    return execFileSync(process.env.PYTHON || 'python', ['-X', 'utf8', '-c', renderPython], {
        cwd: root,
        input: JSON.stringify({
            is_admin: admin,
            body: '<div id="initial">$E=mc^2$ \\[\\int_0^1 x^2 dx\\]</div><div id="problem-content">$x=1$</div><canvas id="chart"></canvas><div id="echart" style="width:400px;height:240px"></div>',
            scripts
        }),
        encoding: 'utf8'
    });
}

async function scenario(browser, name, { admin = false, mobile = false, failEngine = false } = {}) {
    const context = await browser.newContext({ viewport: { width: mobile ? 390 : 1280, height: 800 } });
    const page = await context.newPage();
    const errors = [];
    const external = [];
    const missing = [];
    const fonts = [];
    const html = render(admin, failEngine);
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.hostname !== '127.0.0.1') {
            external.push(url.href);
            return route.abort();
        }
        if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: html });
        if (url.pathname.endsWith('/tex-mml-chtml.js')) {
            if (failEngine) return route.abort();
            // Simulate a cold, slow engine while page scripts already run.
            await new Promise(resolve => setTimeout(resolve, 900));
        }
        if (url.pathname.endsWith('.woff') || url.pathname.endsWith('.woff2')) fonts.push(url.pathname);
        const filename = path.resolve(root, '.' + decodeURIComponent(url.pathname));
        if (!filename.startsWith(root + path.sep) || !fs.existsSync(filename)) {
            missing.push(url.pathname);
            return route.fulfill({ status: 404, body: '' });
        }
        const contentType = { '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff': 'font/woff', '.woff2': 'font/woff2' }[path.extname(filename)] || 'application/octet-stream';
        return route.fulfill({ contentType, body: fs.readFileSync(filename) });
    });
    try {
        await page.goto('http://127.0.0.1/', { waitUntil: 'load' });
        if (failEngine) {
            assert.equal(await page.evaluate(() => window.engineFailure), true);
        } else {
            await page.evaluate(() => window.earlyRender);
            assert.equal(await page.locator('#initial mjx-container').count(), 2);
            assert.equal(await page.locator('#problem-content mjx-container').count(), 1);
            assert.equal(await page.locator('mjx-merror').count(), 0);
            await page.evaluate(() => Promise.all([
                updateProblemText('$a=2$'),
                updateProblemText('$b=3$'),
                updateProblemText('$c=4$')
            ]));
            assert.equal(await page.locator('#problem-content mjx-container').count(), 1);
            assert.equal(await page.evaluate(() => Array.from(MathJax.startup.document.math).length), 3);
            assert.deepEqual(await page.evaluate(() => window.layoutChecks), [true, true, true]);
            // A failed dynamic render must allow the next queued render to run.
            assert.equal(await page.evaluate(async () => {
                const original = MathJax.typesetPromise;
                MathJax.typesetPromise = () => Promise.reject(new Error('simulated typeset failure'));
                let failed = false;
                try { await physicsMath.typeset([document.getElementById('problem-content')]); }
                catch { failed = true; }
                MathJax.typesetPromise = original;
                await updateProblemText('$d=5$');
                return failed;
            }), true);
            assert.ok(fonts.some(url => url.includes('/mathjax-3.2.2/')));
            if (admin) {
                assert.deepEqual(await page.evaluate(() => {
                    const chart = new Chart(document.getElementById('chart'), { type: 'bar', data: { labels: ['A'], datasets: [{ data: [2] }] }, options: { animation: false } });
                    const echart = echarts.init(document.getElementById('echart'));
                    echart.setOption({ animation: false, xAxis: { data: ['A'] }, yAxis: {}, series: [{ type: 'bar', data: [2] }] });
                    gsap.registerPlugin(ScrollTrigger);
                    return [chart.width > 0, echart.getWidth() > 0, typeof ScrollTrigger.create === 'function'];
                }), [true, true, true]);
            }
        }
        assert.deepEqual(errors, []);
        assert.deepEqual(external, []);
        assert.deepEqual(missing, []);
        console.log(`PASS ${name}: no external requests or missing assets`);
    } finally {
        await context.close();
    }
}

(async () => {
    const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
    try {
        await scenario(browser, 'student cold load and concurrent question changes');
        await scenario(browser, 'mobile formula rendering', { mobile: true });
        await scenario(browser, 'admin formulas and chart libraries', { admin: true });
        await scenario(browser, 'engine failure rejects readiness', { failEngine: true });
    } finally {
        await browser.close();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
