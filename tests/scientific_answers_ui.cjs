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
 print(render_template('problem_ajax.html',problem=dict(template_name='科学计数法输入检查',problem_text='<p>请计算题目中各物理量，按顺序填写答案。</p>',answer_units=['m/s','m/s','cm'][:count] if count<=3 else ['N/C']*count,correct_answers=[1]*count,var_values={},var_units={}),answer_count=count,problem_id=4,total_problems=5,total_attempts=0,display_mapping={},display_to_actual={},is_completed=data.get('completed',False),seb_required=False,resumed_problem_url=None))
`;
const cache = new Map();
function render(count, completed=false) {
    const key=`${count}:${completed}`;
    if(!cache.has(key)) cache.set(key, execFileSync(process.env.PYTHON || 'python',['-X','utf8','-c',python],
        {cwd:root,input:JSON.stringify({count,completed}),encoding:'utf8',stdio:['pipe','pipe','pipe']}));
    return cache.get(key);
}

async function scenario(browser,width,count,completed=false) {
    const mobile=width<=768;
    const context=await browser.newContext({viewport:{width,height:900},isMobile:mobile,hasTouch:mobile});
    const page=await context.newPage();
    const errors=[];
    let posts=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',async route=>{
        const url=new URL(route.request().url());
        assert.equal(url.hostname,'exam.test','Unexpected external request');
        if(url.pathname==='/') return route.fulfill({contentType:'text/html',body:render(count,completed)});
        if(url.pathname==='/api/user/completion_status') return route.fulfill({json:{completion_status:{}}});
        if(url.pathname==='/refresh_problem/4') return route.fulfill({json:{success:true,new_var_values:{},new_correct_answers:[2],new_problem_text:'<p>刷新后的题目</p>'}});
        if(url.pathname.startsWith('/api/submit/')) {
            posts.push(route.request().postDataJSON());
            return route.fulfill({status:503,json:{success:false,saved:false,message:'保存失败，当前输入已保留'}});
        }
        if(url.pathname.startsWith('/static/')) {
            const filename=path.resolve(root,'.'+decodeURIComponent(url.pathname));
            assert(filename.startsWith(root+path.sep));
            assert(fs.existsSync(filename),`Missing asset ${url.pathname}`);
            return route.fulfill({path:filename});
        }
        throw new Error('Unexpected request: '+url.pathname);
    });
    await page.goto('https://exam.test/');
    await page.waitForFunction(()=>window.physicsMath && document.getElementById('time-counter'));
    await page.evaluate(()=>window.physicsMath.ready);
    await page.waitForFunction(()=>performance.now()>850);
    assert.equal(await page.locator('[data-answer-row]').count(),count);
    assert.equal(await page.locator('#normal-mode-btn, #scientific-mode-btn, [id^="answer-normal"]').count(),0);
    assert(!await page.locator('#answer-form').textContent().then(text=>text.includes('示例')));
    const bounds=await page.locator('#answer-form').boundingBox();
    assert(bounds.x>=0 && bounds.x+bounds.width<=width+1,`Form overflow at ${width}`);
    const controlBounds=await page.locator('#answer-inputs .answer-control, #answer-inputs label[for^="sign"], [data-exponent-sign]').evaluateAll(nodes=>nodes.map(node=>{
        const r=node.getBoundingClientRect();return {x:r.x,right:r.right,height:r.height,width:r.width};
    }));
    for(const r of controlBounds) assert(r.x>=0 && r.right<=width+1 && r.height>=44 && r.width>=40,`Control layout ${width}: ${JSON.stringify(r)}`);
    if(mobile) assert(!await page.evaluate(()=>document.activeElement.matches('[data-mantissa], [data-exponent]')),'Mobile autofocus');
    if(completed) {
        assert.equal(await page.locator('#mantissa1').isDisabled(),true);
        assert.equal(await page.locator('[data-exponent-sign]').first().isDisabled(),true);
        assert.equal(posts.length,0);
    } else {
        assert.equal(await page.locator('#mantissa1').inputValue(),'');
        assert.equal(await page.locator('#answer1').inputValue(),'');
        await page.locator('#submit-btn').click();
        assert.equal(posts.length,0,'Blank input submitted');
        assert.equal(await page.locator('#submit-btn').isEnabled(),true,'Validation locked the submit button');
        assert.equal(await page.locator('#answer-error1').isVisible(),true);
        await page.locator('#mantissa1').fill('0');
        assert.equal(await page.evaluate(()=>getCurrentAnswer()),true,'Zero answer was treated as blank');
        assert.equal(await page.locator('#answer1').inputValue(),'0');
        const samples=[['4.05','6',false,4050000],['1.20','5',true,-120000],['6.77','-1',false,.677]];
        for(let i=1;i<=count;i++) {
            const [m,e,negative]=samples[(i-1)%3];
            await page.locator(`#mantissa${i}`).fill(m);
            await page.locator(`#exponent${i}`).fill(e);
            if(negative) await page.locator(`label[for="sign-negative${i}"]`).click();
        }
        assert.equal(await page.evaluate(()=>validateInputs()),true);
        const data=await page.evaluate(()=>getAnswerData());
        for(let i=1;i<=count;i++) assert.equal(data[count===1?'answer':`answer${i}`],samples[(i-1)%3][3]);
        if(count>=2) assert.match(await page.locator('#answer-preview2').textContent(),/− 1\.20 × 105/);
        await page.locator('#exponent1').fill('6');
        await page.locator('[data-exponent-sign]').first().click();
        assert.equal(await page.locator('#exponent1').inputValue(),'-6');
        assert.equal(await page.locator('#answer1').inputValue(),'0.00000405');
        await page.locator('[data-exponent-sign]').first().click();
        for(const invalid of ['1.5','', '999']) {
            await page.locator('#exponent1').fill(invalid);
            assert.equal(await page.evaluate(()=>validateInputs()),false);
            assert.equal(await page.locator('#answer1').inputValue(),'');
        }
        await page.locator('#exponent1').fill('6');
        await page.locator('#mantissa1').fill('-2');
        assert.equal(await page.evaluate(()=>validateInputs()),false);
        await page.locator('#mantissa1').fill('4.05');
        if(count>1) {
            await page.locator(`#mantissa${count}`).fill('');
            assert.equal(await page.evaluate(()=>validateInputs()),false,'Partial answer submitted');
            await page.locator(`#mantissa${count}`).fill(samples[(count-1)%3][0]);
        }
        // Enter advances to the exponent, then to the next answer, and finally submits.
        await page.locator('#mantissa1').press('Enter');
        assert.equal(await page.evaluate(()=>document.activeElement.id),'exponent1');
        if(count>1) {
            await page.locator('#exponent1').press('Enter');
            assert.equal(await page.evaluate(()=>document.activeElement.id),'mantissa2');
        }
        await page.locator('#answer-form').screenshot({path:path.join(output,`answers-${width}-${count}.png`)});
        assert.equal(posts.length,0,'Enter advanced and also submitted');
        assert.equal(await page.locator('#submit-btn').isEnabled(),true);
        await page.locator(`#exponent${count}`).press('Enter');
        await page.waitForFunction(()=>!isSubmitting);
        assert.equal(posts.length,1);
        assert.equal(posts[0][count===1?'answer':'answer1'],4050000);
        assert.equal(await page.locator('#mantissa1').inputValue(),'4.05','Save failure lost input');
        await page.evaluate(()=>{disableInputs();});
        assert.equal(await page.locator('#mantissa1').isDisabled(),true);
        assert.equal(await page.locator('[data-exponent-sign]').first().isDisabled(),true);
        await page.evaluate(()=>enableInputs());
        assert.equal(await page.evaluate(()=>refreshProblemParams()),true);
        assert.match(await page.locator('#problem-content').textContent(),/刷新后的题目/);
        for(let i=1;i<=count;i++) {
            assert.equal(await page.locator(`#mantissa${i}`).inputValue(),'');
            assert.equal(await page.locator(`#exponent${i}`).inputValue(),'0');
            assert.equal(await page.locator(`#sign-positive${i}`).isChecked(),true);
            assert.equal(await page.locator(`#answer${i}`).inputValue(),'');
        }
        assert.equal(await page.evaluate(()=>getCurrentAnswer()),false);
    }
    assert.deepEqual(errors,[]);
    await context.close();
    console.log(`Passed: width=${width}, answers=${count}, completed=${completed}`);
}

(async()=>{
    const browser=await chromium.launch({channel:process.env.BROWSER_CHANNEL||'msedge',headless:true});
    try {
        for(const [width,count] of [[1440,3],[1280,1],[1024,2],[768,3],[390,3],[320,6]]) await scenario(browser,width,count);
        await scenario(browser,390,2,true);
    } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
