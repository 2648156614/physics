// Render the actual authoring template, exercise its real JS and local MathJax.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const python = process.env.PYTHON || path.join(root,'.venv-history/Scripts/python.exe');
const fixture = JSON.parse(execFileSync(python,['-X','utf8','-c',`
import json,sys
sys.path.insert(0,'tests')
from test_ai_question_routes import AIRouteTests
t=AIRouteTests(); t.setUp()
try:
    row=t.create()
    page=t.client.get('/admin/ai-questions?paper_id=1').get_data(as_text=True)
    print(json.dumps({'html':page,'draft':row},ensure_ascii=False))
finally:
    t.tearDown()
`],{cwd:root,encoding:'utf8'}));

(async () => {
    const browser = await chromium.launch({channel:'msedge',headless:true});
    try {
        for (const width of [1280,390]) {
            const page = await browser.newPage({viewport:{width,height:900}});
            const errors=[], requests=[];
            let row=structuredClone(fixture.draft), imports=0;
            page.on('pageerror',error=>errors.push(error.message));
            page.on('request',request=>requests.push(request.url()));
            await page.route('https://exam.test/**',async route => {
                const url=new URL(route.request().url());
                const pathname=url.pathname;
                if (pathname.startsWith('/static/')) {
                    const file=path.join(root,pathname.slice(1));
                    const ext=path.extname(file);
                    const mime={'.js':'application/javascript','.css':'text/css','.woff':'font/woff','.woff2':'font/woff2'}[ext] || 'application/octet-stream';
                    return route.fulfill({body:fs.readFileSync(file),contentType:mime});
                }
                if (pathname.endsWith('/import')) {
                    const body=route.request().postDataJSON();
                    assert.equal(body.artifact_hash,row.artifact_hash);
                    assert.equal(body.teacher_confirmed,true);
                    assert.equal(route.request().headers()['x-ai-csrf'],'test-csrf');
                    imports++; row.status='imported'; row.message='已加入题库'; row.template_id=99;
                    return route.fulfill({json:{ok:true,template_id:99}});
                }
                if (pathname.endsWith('/drafts') && route.request().method()==='GET') {
                    return route.fulfill({json:{drafts:[row]}});
                }
                if (pathname.endsWith('/settings')) return route.fulfill({json:{ok:true}});
                if (pathname==='/admin/ai-questions') {
                    const html=fixture.html.replace('<meta name="viewport"', '<script src="/static/js/math-renderer.js"></script><script defer src="/static/vendor/mathjax-3.2.2/es5/tex-mml-chtml.js"></script><meta name="viewport"');
                    return route.fulfill({contentType:'text/html; charset=utf-8',body:html});
                }
                throw new Error('Unexpected request: '+pathname);
            });
            await page.goto('https://exam.test/admin/ai-questions?paper_id=1');
            await page.locator('#ai-result mjx-container').first().waitFor();
            await page.getByRole('button',{name:'换一组预览'}).click();
            await page.waitForFunction(()=>document.querySelector('#ai-result').textContent.includes('10 种目标组合'));
            fs.mkdirSync(path.join(root,'outputs/ai-tests'),{recursive:true});
            await page.screenshot({path:path.join(root,`outputs/ai-tests/authoring-ready-${width}.png`),fullPage:true});
            assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth));
            assert.match(await page.locator('#ai-manual').inputValue(), /"formula"/);
            assert.doesNotMatch(await page.locator('#ai-manual').inputValue(), /valid_cases/);
            const save=page.getByRole('button',{name:'加入当前题库'});
            assert.equal(await save.isDisabled(),true);
            await page.getByRole('checkbox').check();
            await save.click();
            await page.waitForFunction(()=>document.querySelector('#ai-message').textContent.includes('已加入当前题库'));
            assert.equal(imports,1);
            await page.getByRole('button',{name:'新题目',exact:true}).click();
            assert.equal(await page.locator('#ai-text').inputValue(),'');
            await page.getByText('默认生成规范',{exact:true}).click();
            await page.locator('#ai-rules').fill('保持考点，答案尽量为整数');
            await page.getByRole('button',{name:'保存规范'}).click();
            await page.waitForFunction(()=>document.querySelector('#ai-message').textContent.includes('默认规范已保存'));
            assert.deepEqual(errors,[]);
            assert.equal(requests.filter(url=>!url.startsWith('https://exam.test/')).length,0);
            fs.mkdirSync(path.join(root,'outputs/ai-tests'),{recursive:true});
            await page.screenshot({path:path.join(root,`outputs/ai-tests/authoring-${width}.png`),fullPage:true});
            console.log(`AI authoring preview, formula, confirmation, import, rules and layout passed (${width}px)`);
            await page.close();
        }
    } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
