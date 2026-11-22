(function () {
    'use strict';
    function node(tag, text, className) {
        const result=document.createElement(tag);
        if(text!==undefined) result.textContent=text;
        if(className) result.className=className;
        return result;
    }
    document.addEventListener('DOMContentLoaded',()=>{
        document.querySelectorAll('form[data-question-planner]').forEach(async form=>{
            const paper=form.querySelector('[name="paper_id"]');
            const total=form.querySelector('[name="question_count"]');
            const originalCount=Number(total.value)||0;
            paper.required=false;total.required=false;total.min='0';
            paper.closest('.col-md-7').hidden=true;
            total.closest('div').hidden=true;
            const panel=node('section',undefined,'exam-question-planner border rounded p-3');
            panel.setAttribute('aria-label','考试选题方案');
            const heading=node('h6','题库与题目');
            const status=node('p','正在加载题库…','small text-muted');
            const summary=node('div','考试合计：0 题','fw-semibold mt-3');
            const rows=node('div');
            const hidden=node('input');hidden.type='hidden';hidden.name='question_plan';
            panel.append(heading,status,rows,summary,hidden);
            form.querySelector('.row.g-3').before(panel);
            let loaded=false;
            const editors=[];
            form.addEventListener('submit',event=>{
                if(!loaded || !sync()) {event.preventDefault();event.stopImmediatePropagation();panel.scrollIntoView({block:'center'});}
            },true);
            function sync() {
                if(!loaded) return false;
                const selected=[];let error='';
                for(const editor of editors) {
                    if(!editor.toggle.checked)continue;
                    const mandatory=editor.questions.filter(item=>item.input.checked).map(item=>item.id);
                    const random=Number(editor.count.value);
                    if(!/^\d+$/.test(editor.count.value) || random<0 || random>editor.questions.length-mandatory.length || mandatory.length+random<1) {
                        error='请检查各题库的随机题数：不能超过未指定的剩余题数，本库至少出一道题。';
                    }
                    selected.push({paper_id:editor.id,mandatory_ids:mandatory,random_count:random});
                    editor.subtotal.textContent=`本库合计 ${mandatory.length+random} 题 · 未指定 ${editor.questions.length-mandatory.length} 题`;
                }
                selected.sort((a,b)=>a.paper_id-b.paper_id);
                const count=selected.reduce((sum,row)=>sum+row.mandatory_ids.length+row.random_count,0);
                summary.textContent=`考试合计：${count} 题`;
                if(!selected.length)error='请至少选择一个题库并设置题目。';
                status.textContent=error || '勾选的题目必考；随机题从本题库未勾选的题目中抽取。';
                status.className=error?'small text-danger':'small text-muted';
                if(selected.length)paper.value=String(selected[0].paper_id);
                total.removeAttribute('max');total.value=String(count);
                hidden.value=JSON.stringify(selected);
                return !error;
            }
            try {
                const url=new URL('/admin/exams/question-options',location.origin);
                if(form.dataset.examId)url.searchParams.set('exam_id',form.dataset.examId);
                const response=await fetch(url,{cache:'no-store'});
                const data=await response.json();
                if(!response.ok || !data.success)throw Error(data.message || '加载题库失败');
                for(const bank of data.papers) {
                    const selection=(data.selected||[]).find(row=>Number(row.paper_id)===Number(bank.id));
                    const card=node('div',undefined,'border-bottom py-2');
                    const label=node('label',undefined,'form-check fw-semibold');
                    const toggle=node('input');toggle.type='checkbox';toggle.className='form-check-input';toggle.checked=Boolean(selection);
                    label.append(toggle,document.createTextNode(bank.name+(bank.is_enabled?'':'（保存后自动开启）')));
                    const body=node('div',undefined,'mt-2');body.hidden=!toggle.checked;
                    const randomLabel=node('label','额外随机抽题数','form-label small d-block');
                    const count=node('input');count.type='number';count.min='0';count.step='1';count.className='form-control';count.style.maxWidth='160px';
                    count.setAttribute('aria-label',bank.name+'额外随机抽题数');count.value=String(selection?(Number.isInteger(selection.random_count)?selection.random_count:originalCount):0);
                    randomLabel.append(count);
                    const details=node('details');details.append(node('summary','选择必考题（可不选）','small mb-2'));
                    const list=node('div');list.style.maxHeight='220px';list.style.overflowY='auto';
                    const questions=[];
                    for(const question of data.questions.filter(row=>Number(row.paper_id)===Number(bank.id))) {
                        const qlabel=node('label',undefined,'form-check py-1');
                        const input=node('input');input.type='checkbox';input.className='form-check-input';input.checked=Boolean(selection && selection.mandatory_ids.includes(question.id));
                        qlabel.append(input,document.createTextNode(`${question.template_name}（ID ${question.id}）`));
                        list.append(qlabel);questions.push({id:question.id,input});input.addEventListener('change',sync);
                    }
                    const subtotal=node('p','', 'small mt-2 mb-1');
                    if(selection)subtotal.textContent=`本库合计 ${selection.mandatory_ids.length+selection.random_count} 题`;
                    details.append(list);body.append(randomLabel,details,subtotal);card.append(label,body);rows.append(card);
                    editors.push({id:Number(bank.id),toggle,count,questions,subtotal});
                    const updateEnabled=()=>{
                        body.hidden=!toggle.checked;
                        body.querySelectorAll('input').forEach(input=>input.disabled=!toggle.checked);
                    };
                    updateEnabled();
                    toggle.addEventListener('change',()=>{updateEnabled();sync();});count.addEventListener('input',sync);
                }
                loaded=true;
                if(data.locked) {
                    // Metadata-only edits must repost the exact stored policy, including retired IDs.
                    panel.querySelectorAll('input:not([type="hidden"])').forEach(input=>input.disabled=true);
                    hidden.value=data.has_plan?JSON.stringify(data.selected):'';
                    if(!data.has_plan)hidden.removeAttribute('name');
                    summary.textContent=`考试合计：${originalCount} 题`;
                    status.textContent='已有学生进入，选题方案已锁定；名单、时间和 SEB 配置仍可调整。';
                    sync=()=>true;
                } else sync();
            } catch(error) {
                status.textContent=error.message+'，请刷新后重试。';status.className='small text-danger';
            }
        });
    });
})();
