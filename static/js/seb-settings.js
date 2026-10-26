document.querySelectorAll('select[name="seb_config_mode"]').forEach(function (select) {
    const panel = select.parentElement.querySelector('[data-seb-independent]');
    if (!panel) return;
    function update() {
        panel.hidden = select.value === 'shared';
        panel.querySelectorAll('input').forEach(function (input) {
            input.disabled = panel.hidden;
        });
    }
    select.addEventListener('change', update);
    update();
});
