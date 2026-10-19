// Local browser checks; all HTTP requests use fixtures, never production services.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const asset = name => path.join(root, name);
const source = fs.readFileSync(asset('templates/problem_ajax.html'), 'utf8');
const submissionCode = source.slice(source.indexOf('let unresolvedSubmissionId = null;'), source.indexOf('// 下一题按钮'));

async function previewCheck(browser, width) {
    const page = await browser.newPage({viewport: {width, height: 900}});
    let saved = 0;
    await page.route('https://exam.test/**', async route => {
        const url = route.request().url();
        if (url.endsWith('/admin/exams/roster-preview')) {
            assert.match(route.request().postData(), /update_exam/);
            return route.fulfill({json: {success:true,mode:'replace',can_confirm:true,token:'reviewed-token',duplicate_count:0,
                added:[{id:3,username:'1003',name:'<img src=x onerror="window.unsafe=true">'}],retained:[],
                removed:[{id:1,username:'1001',name:'原学生'}],missing:[{row_no:3,username:'9999',name:'未注册',reason:'未找到对应的系统账户'}],conflicts:[]}});
        }
        if (url.endsWith('/save')) {
            saved++;
            assert.match(route.request().postData(), /reviewed-token/);
            return route.fulfill({contentType:'text/html',body:'saved'});
        }
        return route.fulfill({contentType:'text/html',body:`<form method="post" action="/save" enctype="multipart/form-data" data-roster-preview="update_exam" data-exam-id="9">
            <select name="assign_type"><option value="student">Excel</option></select><input type="file" name="student_file">
            <select name="student_import_mode"><option value="replace">替换</option></select><button type="submit">保存修改</button></form>`});
    });
    await page.goto('https://exam.test/');
    await page.addStyleTag({path:asset('static/vendor/bootstrap-5.1.3/css/bootstrap.min.css')});
    await page.addScriptTag({path:asset('static/vendor/bootstrap-5.1.3/js/bootstrap.bundle.min.js')});
    await page.addScriptTag({path:asset('static/js/exam-roster-preview.js')});
    await page.locator('[name="student_file"]').setInputFiles({name:'students.xlsx',mimeType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',buffer:Buffer.from('fixture')});
    await page.locator('form button').click();
    await page.getByText('新增 1 人，保留 0 人，移出 1 人，未匹配 1 行。').waitFor();
    assert.equal(saved,0);
    assert.equal(await page.evaluate(() => !!window.unsafe),false);
    const bounds=await page.locator('.modal-dialog').boundingBox();
    assert(bounds.width <= width);
    await page.getByText('返回修改',{exact:true}).click();
    await page.locator('.modal').waitFor({state:'hidden'});
    assert.equal(saved,0);
    await page.locator('form button').click();
    await page.locator('[data-confirm-roster]:enabled').waitFor();
    await page.getByText('确认名单并保存',{exact:true}).click();
    await page.waitForURL('**/save');
    assert.equal(saved,1);
    await page.close();
}

async function submissionCheck(browser, mode) {
    const page=await browser.newPage();
    page.on('pageerror',error=>console.error('Submission page error:',mode,error.message));
    let posts=0,checks=0;
    await page.route('https://exam.test/**',async route => {
        const url=route.request().url();
        if(url.includes('/api/submit/')) {
            posts++;
            if(mode==='recover' || mode==='unknown') return route.abort('failed');
            return route.fulfill({status:mode==='login'?401:mode==='save-failed'?503:403,
                json:{success:false,saved:false,message:mode==='save-failed'?'答案未能保存，请重新提交。':mode==='login'?'登录已失效':'SEB 配置需要更新'}});
        }
        if(url.includes('/api/submissions/')) {
            checks++;
            return route.fulfill({json:mode==='recover'?{state:'done',data:{success:true,correct:false,total_attempts:1,correct_answers:[4],response_ids:[101]}}:{state:'unknown'}});
        }
        return route.fulfill({contentType:'text/html',body:'<form id="answer-form"><input id="answer1" value="12.5"><button id="submit-btn" type="submit">提交答案</button></form><button id="next-btn"></button>'});
    });
    await page.goto('https://exam.test/');
    await page.addScriptTag({path:asset('static/js/answer-submission.js')});
    await page.evaluate(() => {
        Object.assign(window,{isSubmitting:false,problemId:2,totalProblems:3,sebRequired:true,totalAttempts:0,timerInterval:null,messages:[],result:null,currentInputMode:'normal',currentCorrectAnswers:[4],
            validateInputs:()=>true,getAnswerData:()=>({answer:parseFloat(document.getElementById('answer1').value),time_taken:5}),
            showMessage:message=>window.messages.push(message),handleCompletedProblem:()=>{},disableInputs:()=>{},
            updateProblemSidebar:()=>{},updateAttemptsDisplay:()=>{},formatCorrectAnswers:()=>'',showErrorCorrection:()=>{},
            scheduleProblemRegeneration:data=>{window.result=data;window.resetSubmitButton();},
            resetSubmitButton:()=>{window.isSubmitting=false;const button=document.getElementById('submit-btn');button.disabled=false;button.textContent='提交答案';}
        });
    });
    await page.addScriptTag({content:submissionCode});
    await page.locator('#submit-btn').click();
    if(mode==='unknown') {
        await page.getByText('再次核对提交结果',{exact:true}).waitFor();
        assert.equal(posts,1);
        assert.equal(checks,4);
        await page.locator('#submit-btn').click();
        await page.getByText('再次核对提交结果',{exact:true}).waitFor();
        await page.locator('#submit-btn:enabled').waitFor();
        assert.equal(posts,1);
        assert.equal(checks,8);
    } else if(mode==='recover') {
        await page.waitForFunction(()=>window.result?.total_attempts===1);
        assert.equal(posts,1);
        assert.equal(checks,1);
    } else {
        try { await page.waitForFunction(()=>window.messages.length>0 && !window.isSubmitting, null, {timeout:5000}); }
        catch(error) { console.error(mode,{posts,checks},await page.evaluate(()=>({messages:window.messages,submitting:window.isSubmitting,button:document.getElementById('submit-btn').textContent}))); throw error; }
        const messages=await page.evaluate(()=>window.messages.join(' '));
        assert.match(messages,mode==='login'?/登录已失效/:mode==='save-failed'?/答案未能保存/:/SEB 配置需要更新/);
        assert.equal(checks,0);
        assert.equal(await page.locator('#submit-btn').isEnabled(),true);
    }
    assert.equal(await page.locator('#answer1').inputValue(),'12.5');
    await page.close();
}

(async()=>{
    const browser=await chromium.launch({channel:process.env.BROWSER_CHANNEL || 'msedge',headless:true});
    try {
        await previewCheck(browser,1280);
        await previewCheck(browser,390);
        for(const mode of ['seb','login','save-failed','recover','unknown']) await submissionCheck(browser,mode);
        console.log('7 browser scenarios passed: desktop/mobile roster confirmation, specific errors, recovery and no duplicate POST.');
    } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
