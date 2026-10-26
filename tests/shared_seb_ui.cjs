// Render the real templates and verify shared/independent controls in desktop and mobile Edge.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const python = `
import json,sys
from jinja2 import Environment,FileSystemLoader,DictLoader,ChoiceLoader,StrictUndefined
sys.path.insert(0,'tests')
from test_exam_arrangement_routes import ArrangementRouteTests
f=ArrangementRouteTests(); f.setUp()
env=Environment(loader=ChoiceLoader([DictLoader({'base.html':'<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1"><main>{% block content %}{% endblock %}</main>{% block scripts %}{% endblock %}'}),FileSystemLoader('templates')]),undefined=StrictUndefined,autoescape=True)
def url_for(endpoint,**kw):
    return '/static/'+kw['filename'] if endpoint=='static' else '/'+endpoint
env.globals['url_for']=url_for
exam=f.exam(); exam.update(editable_question_count=2,paper_name='paper',batch_count=2,student_count=1)
context=dict(editing_exam=exam,exams=[exam],exam_papers=[dict(id=10,template_count=3,is_enabled=True,name='paper')],class_options=['A'],major_options=['P'],course_options=['C'],retake_candidates=[],editing_assignments=[],editing_assign_type='class',editing_assign_values=['A'],selected_source_exam_id=None,previous_seb_key_count=1,shared_seb=dict(seb_config_key=None,seb_quit_token='shared-token'))
admin=env.get_template('admin_exams.html').render(**context)
exams=[dict(name='<img src=x onerror=alert(1)>',id=9,batch_name='上午场',start_time=None,end_time=None,access_allowed=True,access_status='open'),dict(name='下午考试',id=10,batch_name='下午场',start_time=None,end_time=None,access_allowed=False,access_status='not_started')]
selection=env.get_template('seb_exams.html').render(exams=exams)
print(json.dumps(dict(admin=admin,selection=selection)))
f.tearDown()
`;
const html = JSON.parse(execFileSync(process.env.PYTHON || 'python', ['-X','utf8','-c',python], {cwd:root,encoding:'utf8'}));
(async () => {
    const browser = await chromium.launch({channel:'msedge',headless:true});
    try {
        for (const width of [1280,390]) {
            const page = await browser.newPage({viewport:{width,height:900}});
            await page.route('https://exam.test/**', async route => {
                const pathname = new URL(route.request().url()).pathname;
                if (pathname.startsWith('/static/')) {
                    const file = path.join(root, pathname.slice(1));
                    return route.fulfill({body:fs.readFileSync(file),contentType:'application/javascript'});
                }
                return route.fulfill({contentType:'text/html; charset=utf-8',body:pathname==='/selection'?html.selection:html.admin});
            });
            await page.goto('https://exam.test/admin');
            await page.addStyleTag({path:path.join(root,'static/vendor/bootstrap-5.1.3/css/bootstrap.min.css')});
            const selects = page.locator('select[name="seb_config_mode"]');
            assert.equal(await selects.nth(0).inputValue(), 'shared');
            assert.equal(await page.locator('[data-seb-independent]').nth(0).isVisible(), false);
            assert.equal(await selects.nth(1).inputValue(), 'exam');
            assert.equal(await page.locator('[data-seb-independent]').nth(1).isVisible(), true);
            await selects.nth(1).selectOption('shared');
            assert.equal(await page.locator('[data-seb-independent]').nth(1).isVisible(), false);
            await selects.nth(1).selectOption('exam');
            assert.equal(await page.locator('#edit-seb-config-key').inputValue(), 'a'.repeat(64));
            await page.locator('summary').click();
            await page.locator('#shared-key').fill('b'.repeat(64));
            assert.equal(await page.locator('#shared-key').evaluate(el=>el.checkValidity()), true);
            await page.goto('https://exam.test/selection');
            await page.addStyleTag({path:path.join(root,'static/vendor/bootstrap-5.1.3/css/bootstrap.min.css')});
            assert.equal(await page.locator('a.btn-primary').count(), 1);
            assert.equal(await page.locator('img').count(), 0);
            assert.match(await page.locator('body').innerText(), /考试尚未开始/);
            assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth));
            await page.close();
            console.log(`Shared SEB controls and selection passed (${width}px)`);
        }
    } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
