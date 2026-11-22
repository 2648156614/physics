// Real Jinja template and planner; requests are isolated fixtures, never production.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const python = `
import json,sys
from pathlib import Path
from jinja2 import ChoiceLoader,DictLoader,Environment,FileSystemLoader,StrictUndefined
data=json.load(sys.stdin)
env=Environment(loader=ChoiceLoader([DictLoader({'base.html':'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/vendor/bootstrap-5.1.3/css/bootstrap.min.css"></head><body><main class="container py-3">{% block content %}{% endblock %}</main><script src="/static/vendor/bootstrap-5.1.3/js/bootstrap.bundle.min.js"></script>{% block scripts %}{% endblock %}</body></html>'''}),FileSystemLoader('templates')]),undefined=StrictUndefined,autoescape=True)
def url_for(endpoint,**kw):
    if endpoint=='static':return '/static/'+kw['filename']
    if endpoint=='admin.admin_create_exam':return '/save'
    return '/'+endpoint
env.globals['url_for']=url_for
exam=dict(id=9,name='existing',paper_id=10,question_count=8,editable_question_count=8,exam_type='normal',start_time=None,end_time=None,status='published',seb_required=False,seb_config_key=None,seb_previous_config_keys=None,seb_quit_token='token',seb_config_mode='shared',paper_name='bank A、bank B',batch_count=1,student_count=1)
context=dict(editing_exam=exam if data.get('edit') else None,exams=[],exam_papers=[dict(id=10,template_count=5,is_enabled=True,name='bank A'),dict(id=20,template_count=8,is_enabled=False,name='bank B')],class_options=['A'],major_options=['P'],course_options=['C'],retake_candidates=[],editing_assignments=[],editing_assign_type='class',editing_assign_values=['A'],selected_source_exam_id=None,previous_seb_key_count=0,shared_seb=dict(seb_config_key=None,seb_quit_token='token'))
print(env.get_template('admin_exams.html').render(**context))
`;
const selected = [{paper_id:10,mandatory_ids:[101],random_count:2},
                  {paper_id:20,mandatory_ids:[201,202],random_count:3}];
function render(edit) {
    return execFileSync(process.env.PYTHON || 'python', ['-X','utf8','-c',python],
        {cwd:root,input:JSON.stringify({edit}),encoding:'utf8'});
}
async function scenario(browser,width,mode) {
    const page = await browser.newPage({viewport:{width,height:1000}});
    page.setDefaultTimeout(10000);
    const errors=[]; let saved;
    page.on('pageerror',e=>{errors.push(e.message);console.error('Planner browser error:',e.message);});
    await page.route('**/*', async route=>{
        const url = new URL(route.request().url());
        assert.equal(url.hostname,'exam.test');
        if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:render(mode==='locked')});
        if(url.pathname==='/admin/exams/question-options') {
            if(mode==='failed')return route.fulfill({status:503,json:{success:false,message:'题库暂不可用'}});
            return route.fulfill({json:{success:true,
                papers:[{id:10,name:'bank A',is_enabled:true},{id:20,name:'bank B',is_enabled:false}],
                questions:[...[101,102,103,104,105].map(id=>({id,paper_id:10,template_name:'物理题目 '+id})),
                    ...[201,202,203,204,205,206,207,208].map(id=>({id,paper_id:20,template_name:'很长的物理题目名称用于检查手机端换行与触摸操作 '+id}))],
                selected:url.searchParams.has('exam_id')?selected:null,
                locked:url.searchParams.has('exam_id'),has_plan:url.searchParams.has('exam_id')}});
        }
        if(url.pathname==='/save') {
            saved=new Map([...route.request().postData().matchAll(/name="([^"]+)"\r\n\r\n([\s\S]*?)\r\n--/g)]
                .map(match=>[match[1],match[2]]));
            return route.fulfill({contentType:'text/html',body:'saved'});
        }
        if(url.pathname.startsWith('/static/')) {
            const filename=path.resolve(root,'.'+url.pathname);
            assert(filename.startsWith(root+path.sep));
            return route.fulfill({path:filename});
        }
        throw Error('Unexpected request '+url);
    });
    await page.goto('https://exam.test/');
    const form=page.locator(mode==='locked'?'form[data-question-planner][data-exam-id]':'form[data-question-planner]').first();
    const panel=form.locator('.exam-question-planner');
    await panel.waitFor();
    if(mode==='failed') {
        await panel.getByText('题库暂不可用，请刷新后重试。').waitFor();
        assert.equal(await form.evaluate(el=>el.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))),false);
    } else if(mode==='locked') {
        await panel.getByText(/选题方案已锁定/).waitFor();
        assert.deepEqual(JSON.parse(await form.locator('[name=question_plan]').inputValue()), selected);
        assert.equal(await panel.locator('input:not([type=hidden]):enabled').count(),0);
        assert.equal(await panel.getByText('考试合计：8 题').count(),1);
    } else {
        await panel.getByText('bank A',{exact:true}).waitFor();
        await panel.getByText('bank A',{exact:true}).click();
        await panel.locator('details summary').first().click();
        await panel.getByText('物理题目 101（ID 101）',{exact:true}).click();
        await panel.getByLabel('bank A额外随机抽题数').fill('2');
        await panel.getByText('bank B（保存后自动开启）',{exact:true}).click();
        await panel.locator('details summary').nth(1).click();
        await panel.locator('label').filter({hasText:'ID 201）'}).click();
        await panel.locator('label').filter({hasText:'ID 202）'}).click();
        const random=panel.getByLabel('bank B额外随机抽题数');
        await random.fill('7');
        assert.equal(await form.evaluate(el=>el.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))),false);
        await panel.getByText(/请检查各题库/).waitFor();
        await random.fill('3');
        assert.deepEqual(JSON.parse(await form.locator('[name=question_plan]').inputValue()),selected);
        await panel.getByText('考试合计：8 题').waitFor();
        const bounds=await panel.boundingBox();
        assert(bounds.x>=0 && bounds.x+bounds.width<=width+1);
        assert.equal(await panel.evaluate(el=>el.scrollWidth<=el.clientWidth+1),true);
        // Deselecting a bank also disables hidden invalid number fields.
        await random.fill('-1');
        await panel.getByText('bank B（保存后自动开启）',{exact:true}).click();
        assert.equal(await random.isDisabled(),true);
        await panel.getByText('bank B（保存后自动开启）',{exact:true}).click();
        await random.fill('3');
        await form.locator('[name=name]').fill('two bank exam');
        await form.locator('[name=assign_value][value=A]').check();
        await form.locator('[type=submit]').last().click();
        await page.waitForURL('**/save');
        assert.equal(saved.get('question_count'),'8');
        assert.deepEqual(JSON.parse(saved.get('question_plan')),selected);
    }
    assert.deepEqual(errors,[]);
    await page.close();
}
(async()=>{
    const browser=await chromium.launch({channel:process.env.BROWSER_CHANNEL||'msedge',headless:true});
    try {
        for(const width of [375,1280])for(const mode of ['create','locked','failed'])await scenario(browser,width,mode);
        console.log('Multi-bank planner: 6 desktop/mobile scenarios passed.');
    } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
