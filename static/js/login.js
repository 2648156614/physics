(function () {
    const passwordInput = document.getElementById('password');
    const passwordToggle = document.getElementById('password-toggle');
    const loginForm = document.getElementById('login-form');
    const loginSubmit = document.getElementById('login-submit');

    if (window.matchMedia('(min-width: 769px)').matches) {
        document.getElementById('username')?.focus();
    }

    if (passwordInput && passwordToggle) {
        passwordToggle.addEventListener('click', function () {
            const shouldShow = passwordInput.type === 'password';
            passwordInput.type = shouldShow ? 'text' : 'password';
            passwordToggle.setAttribute('aria-pressed', shouldShow ? 'true' : 'false');
            passwordToggle.setAttribute('aria-label', shouldShow ? '隐藏密码' : '显示密码');

            const icon = passwordToggle.querySelector('i');
            if (icon) {
                icon.className = shouldShow ? 'bi bi-eye-slash' : 'bi bi-eye';
            }
        });
    }

    if (loginForm && loginSubmit) {
        loginForm.addEventListener('submit', function () {
            loginSubmit.disabled = true;
            loginSubmit.querySelector('span').textContent = '正在登录';
        });
    }
})();
