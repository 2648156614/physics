// static/js/mouse-trail.js
class MouseTrail {
    constructor() {
        this.canvas = null;
        this.ctx = null;
        this.particles = [];
        this.mouseX = 0;
        this.mouseY = 0;
        this.isDrawing = false;

        this.init();
    }

    init() {
        // 创建canvas元素
        this.canvas = document.createElement('canvas');
        this.canvas.id = 'mouse-trail-canvas';
        this.canvas.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            pointer-events: none;
            z-index: 9999;
        `;

        document.body.appendChild(this.canvas);
        this.ctx = this.canvas.getContext('2d');

        this.resize();
        this.bindEvents();
        this.animate();
    }

    resize() {
        this.canvas.width = window.innerWidth;
        this.canvas.height = window.innerHeight;
    }

    bindEvents() {
        // 鼠标移动事件
        document.addEventListener('mousemove', (e) => {
            this.mouseX = e.clientX;
            this.mouseY = e.clientY;
            this.createParticles(e.clientX, e.clientY);
        });

        // 窗口大小调整
        window.addEventListener('resize', () => this.resize());

        // 鼠标进入/离开页面
        document.addEventListener('mouseenter', () => this.isDrawing = true);
        document.addEventListener('mouseleave', () => this.isDrawing = false);
    }

    createParticles(x, y) {
        if (!this.isDrawing) return;

        const particleCount = 3;
        for (let i = 0; i < particleCount; i++) {
            this.particles.push({
                x: x + (Math.random() - 0.5) * 10,
                y: y + (Math.random() - 0.5) * 10,
                size: Math.random() * 2 + 1,
                speedX: (Math.random() - 0.5) * 2,
                speedY: (Math.random() - 0.5) * 2,
                color: this.getRandomColor(),
                life: 1,
                decay: Math.random() * 0.02 + 0.01
            });
        }

        // 限制粒子数量
        if (this.particles.length > 100) {
            this.particles = this.particles.slice(-80);
        }
    }

    getRandomColor() {
        const colors = [
            'rgba(66, 133, 244, 0.8)',  // 蓝色
            'rgba(234, 67, 53, 0.8)',   // 红色
            'rgba(251, 188, 5, 0.8)',   // 黄色
            'rgba(52, 168, 83, 0.8)',   // 绿色
            'rgba(171, 71, 188, 0.8)'   // 紫色
        ];
        return colors[Math.floor(Math.random() * colors.length)];
    }

    animate() {
        this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);

        // 更新和绘制粒子
        for (let i = this.particles.length - 1; i >= 0; i--) {
            const p = this.particles[i];

            // 更新位置
            p.x += p.speedX;
            p.y += p.speedY;

            // 更新生命周期
            p.life -= p.decay;

            // 移除死亡的粒子
            if (p.life <= 0) {
                this.particles.splice(i, 1);
                continue;
            }

            // 绘制粒子
            this.ctx.save();
            this.ctx.globalAlpha = p.life;
            this.ctx.fillStyle = p.color;
            this.ctx.beginPath();
            this.ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
            this.ctx.fill();
            this.ctx.restore();
        }

        requestAnimationFrame(() => this.animate());
    }

    // 可选：在特定事件时创建特效
    createBurst(x, y) {
        for (let i = 0; i < 15; i++) {
            this.particles.push({
                x: x,
                y: y,
                size: Math.random() * 3 + 1,
                speedX: (Math.random() - 0.5) * 6,
                speedY: (Math.random() - 0.5) * 6,
                color: this.getRandomColor(),
                life: 1,
                decay: Math.random() * 0.03 + 0.02
            });
        }
    }
}

// 初始化鼠标轨迹
document.addEventListener('DOMContentLoaded', () => {
    const mouseTrail = new MouseTrail();

    // 在表单提交等事件时添加特效
    document.addEventListener('click', (e) => {
        if (e.target.type === 'submit' || e.target.tagName === 'BUTTON') {
            mouseTrail.createBurst(e.clientX, e.clientY);
        }
    });

    // 答题正确时的特效
    window.addEventListener('answerCorrect', () => {
        // 在屏幕中央创建庆祝特效
        for (let i = 0; i < 30; i++) {
            mouseTrail.createBurst(
                window.innerWidth / 2,
                window.innerHeight / 2
            );
        }
    });

    // 暴露到全局，以便其他脚本调用
    window.mouseTrail = mouseTrail;
});