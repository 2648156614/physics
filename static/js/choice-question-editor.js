(function () {
    'use strict';
    const strategy=document.querySelector('[name=generation_strategy]');
    if(!strategy)return;
    const form=strategy.form;
    let original={};
    try {original=JSON.parse(strategy.value||'{}');}catch(_){}
    const section=document.createElement('section');
    section.className='border rounded p-3 mb-3';
    section.innerHTML='<label class="form-label">题型<select name="question_type" class="form-select"><option value="calculation">计算题</option><option value="single_choice">单选题</option><option value="multiple_choice">多选题</option></select></label><div data-choice-editor><label class="form-label d-block">选项数量<select name="choice_count" class="form-select"></select></label><p>勾选正确答案。单选题选一个，多选题选多个。每个选项可填写文字或上传图片。</p><div data-choice-options></div><button type="button" class="btn btn-outline-secondary" data-choice-preview>预览选择题</button><div data-choice-preview-body class="mt-3"></div></div>';
    form.prepend(section);
    const type=section.querySelector('[name=question_type]');
    const count=section.querySelector('[name=choice_count]');
    for(let i=2;i<=8;i++)count.add(new Option(String(i),String(i)));
    const options=section.querySelector('[data-choice-options]');
    for(let i=1;i<=8;i++) {
        const row=document.createElement('div');row.className='border rounded p-2 mb-2';row.dataset.index=i;
        row.innerHTML=`<label class="form-check py-2"><input class="form-check-input" type="checkbox" name="choice_correct" value="${i}"> ${String.fromCharCode(64+i)} 为正确答案</label><textarea class="form-control mb-2" name="choice_text_${i}" aria-label="选项 ${i} 文字" rows="2"></textarea><label class="form-label d-block">选项 ${i} 图片<input class="form-control" type="file" name="choice_image_${i}" accept=".png,.jpg,.jpeg,.gif,.webp"></label><input type="hidden" name="choice_image_existing_${i}"><img class="img-fluid" style="max-height:180px" alt="已有选项图片" hidden>`;
        const saved=(original.options||[])[i-1];
        if(saved){row.querySelector('textarea').value=saved.text||'';row.querySelector('[type=hidden]').value=saved.image_filename||'';row.querySelector('[type=checkbox]').checked=(original.correct_ids||[]).includes(i);if(saved.image_filename){const img=row.querySelector('img');img.src='/problem_image/'+encodeURIComponent(saved.image_filename);img.hidden=false;}}
        options.append(row);
    }
    type.value=original.mode==='choice_v1'?original.question_type:'calculation';count.value=String((original.options||[]).length||4);
    const numericNames=['variables','solution_formula','answer_count','answer_units','generation_strategy'];
    function update(){
        const choice=type.value!=='calculation';
        section.querySelector('[data-choice-editor]').hidden=!choice;
        form.querySelector('[name=problem_text]').required=!choice;
        for(const name of numericNames){const field=form.querySelector(`[name="${name}"]`);if(field){const wrap=field.closest('.mb-3')||field.parentElement;wrap.hidden=choice;field.disabled=choice;}}
        for(const row of options.children){row.hidden=Number(row.dataset.index)>Number(count.value);row.querySelectorAll('input,textarea').forEach(el=>el.disabled=!choice||row.hidden);}
        if(!choice&&original.mode==='choice_v1'&&strategy.value.includes('choice_v1'))strategy.value='';
    }
    type.addEventListener('change',update);count.addEventListener('change',update);update();
    section.querySelector('[data-choice-preview]').addEventListener('click',()=>{
        const preview=section.querySelector('[data-choice-preview-body]');preview.replaceChildren();
        const stem=document.createElement('p');stem.textContent=form.querySelector('[name=problem_text]').value;preview.append(stem);
        function picture(file,parent){if(!file)return;const img=document.createElement('img');img.className='img-fluid d-block mb-2';img.style.maxHeight='240px';img.src=URL.createObjectURL(file);img.onload=()=>URL.revokeObjectURL(img.src);parent.append(img);}
        picture(form.querySelector('[name=problem_image]')?.files[0],preview);
        for(const row of options.children){if(row.hidden)continue;const p=document.createElement('p');p.textContent=String.fromCharCode(64+Number(row.dataset.index))+'. '+row.querySelector('textarea').value;preview.append(p);const file=row.querySelector('[type=file]').files[0];if(file)picture(file,preview);else if(!row.querySelector('img').hidden)preview.append(row.querySelector('img').cloneNode());}
    });
})();
