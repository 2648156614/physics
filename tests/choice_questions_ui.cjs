// Full-page browser checks with fixture data and intercepted requests only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const output = path.join(root, '.tmp-scientific-input');
fs.mkdirSync(output, {recursive:true});
const python = `
import os,socket,json,sys
os.environ['SECRET_KEY']='fixture-only'
os.environ['REQUIRE_REDIS']='false'
def offline(*args,**kwargs): raise OSError('offline verification')
socket.socket.connect=offline
from app import app
from flask import render_template,session
data=json.load(sys.stdin)
count=data['count']
with app.test_request_context('/problem_ajax/4'):
 session.update(user_id=1,username='fixture')
 if count==5:
  print(render_template('admin_add_problem.html',exam_papers=[dict(id=1,name='fixture')],selected_paper_id=1));sys.exit(0)
 print(render_template('problem_ajax.html',problem=dict(template_name='科学计数法输入检查',problem_text='<p>请计算题目中各物理量，按顺序填写答案。</p>',answer_units=['m/s','m/s','cm'][:count] if count<=3 else ['N/C']*count,correct_answers=[5],var_values={},var_units={},question_type=('single_choice' if count==1 else 'multiple_choice'),choice_options=[dict(id=i,text='选项 '+str(i),image_filename=('fixture.png' if i==1 else '')) for i in range(1,5)]),answer_count=count,problem_id=4,total_problems=5,total_attempts=0,display_mapping={},display_to_actual={},is_completed=data.get('completed',False),seb_required=False,resumed_problem_url=None))
`;
const cache = new Map();
function render(count, completed=false) {
    const key=`${count}:${completed}`;
    if(!cache.has(key)) cache.set(key, execFileSync(process.env.PYTHON || 'python',['-X','utf8','-c',python],
        {cwd:root,input:JSON.stringify({count,completed}),encoding:'utf8',stdio:['pipe','pipe','pipe']}));
    return cache.get(key);
}


async function scenario(browser,width,count){
 const page=await browser.newPage({viewport:{width,height:900}});const errors=[];let payload;
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url());assert.equal(u.hostname,'exam.test');
  if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:render(count)});
  if(u.pathname==='/problem_image/fixture.png')return route.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j6V8AAAAASUVORK5CYII=','base64')});
  if(u.pathname.startsWith('/api/submit/')){payload=route.request().postDataJSON();return route.fulfill({status:503,json:{success:false,saved:false,message:'保存失败，输入已保留'}});}
  if(u.pathname.startsWith('/api/'))return route.fulfill({json:{completion_status:{}}});
  if(u.pathname.startsWith('/static/'))return route.fulfill({path:path.join(root,decodeURIComponent(u.pathname))});
  throw Error(u.pathname);
 });
 await page.goto('https://exam.test/');await page.waitForFunction(()=>window.physicsMath);await page.evaluate(()=>window.physicsMath.ready);
 assert.equal(await page.locator('[data-mantissa]').count(),0);
 assert.equal(await page.locator('[name=choice-answer]').count(),4);
 await page.locator('[name=choice-answer][value="1"]').check();await page.locator('[name=choice-answer][value="3"]').check();
 const expected=count===1?[3]:[1,3];
 assert.deepEqual(await page.evaluate(()=>getAnswerData().selected_ids),expected);
 assert.equal(await page.locator('#answer-preview1').textContent(),count===1?'C':'A、C');
 await page.locator('.choice-answer-option img').click();await page.locator('dialog[open]').waitFor();await page.getByText('关闭图片',{exact:true}).click();
 assert.deepEqual(await page.evaluate(()=>getAnswerData().selected_ids),expected);
 const bounds=await page.locator('#answer-inputs').boundingBox();assert(bounds.x>=0&&bounds.x+bounds.width<=width+1);
 await page.evaluate(()=>disableInputs());assert(await page.locator('[name=choice-answer]').first().isDisabled());await page.evaluate(()=>enableInputs());
 await page.locator('#submit-btn').click();await page.waitForTimeout(1200);assert.deepEqual(payload.selected_ids,expected);
 assert.deepEqual(await page.evaluate(()=>getAnswerData().selected_ids),expected);
 assert.deepEqual(errors,[]);await page.close();
}

async function editor(browser,width){
 const page=await browser.newPage({viewport:{width,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',route=>{const u=new URL(route.request().url());if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:render(5)});if(u.pathname.startsWith('/static/'))return route.fulfill({path:path.join(root,decodeURIComponent(u.pathname))});throw Error(u.pathname);});
 await page.goto('https://exam.test/');await page.locator('[name=question_type]').waitFor();
 await page.locator('[name=question_type]').selectOption('single_choice');
 assert(await page.locator('[name=solution_formula]').isDisabled());
 await page.locator('[name=choice_text_1]').fill('选项文字');await page.locator('[name=choice_correct][value="1"]').check();
 await page.locator('[name=problem_text]').fill('这是题干');await page.locator('[data-choice-preview]').click();
 assert.match(await page.locator('[data-choice-preview-body]').textContent(),/这是题干.*选项文字/);
 await page.locator('[name=question_type]').selectOption('calculation');assert(!await page.locator('[name=solution_formula]').isDisabled());
 assert.deepEqual(errors,[]);await page.close();
}
(async()=>{const browser=await chromium.launch({channel:'msedge',headless:true});try{for(const width of [375,1280])for(const count of [1,2])await scenario(browser,width,count);for(const width of [375,1280])await editor(browser,width);console.log('6 choice browser scenarios passed');}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
