class ChoiceAnswerInput {
    constructor(root) {
        this.root=root;
        root.addEventListener('change',()=>this.preview());
        const dialog=document.createElement('dialog');dialog.className='choice-image-dialog';
        const close=document.createElement('button');close.type='button';close.className='btn btn-secondary d-block mb-2';close.textContent='关闭图片';close.onclick=()=>dialog.close();
        const image=document.createElement('img');image.alt='放大的题目图片';dialog.append(close,image);document.body.append(dialog);
        const enlarge=event=>{if(event.target.tagName!=='IMG')return;event.preventDefault();event.stopPropagation();image.src=event.target.src;dialog.showModal();};
        root.addEventListener('click',enlarge);document.getElementById('problem-content')?.addEventListener('click',enlarge);
        this.preview();
    }
    static labels(value){return [...'ABCDEFGH'].filter((_,i)=>Number(value)&(1<<i)).join('、')||'未选择';}
    selected(){return [...this.root.querySelectorAll('[name=choice-answer]:checked')].map(input=>Number(input.value));}
    values(){return [this.selected().reduce((value,id)=>value+(1<<(id-1)),0)];}
    validate(){const ok=this.selected().length>0;this.root.querySelector('[data-choice-error]').textContent=ok?'':'请选择答案后提交';return ok?null:'请选择答案后提交';}
    clear(){this.root.querySelectorAll('[name=choice-answer]').forEach(input=>input.checked=false);this.preview();}
    setDisabled(disabled){this.root.querySelectorAll('[name=choice-answer]').forEach(input=>input.disabled=disabled);}
    hasInput(){return this.selected().length>0;}
    preview(){const preview=document.getElementById('answer-preview1');if(preview){preview.textContent=ChoiceAnswerInput.labels(this.values()[0]);preview.classList.toggle('is-empty',!this.hasInput());}}
}
