// static/js/elegant-water-beads.js
class ElegantWaterBeads {
    constructor() {
        this.canvas = null;
        this.ctx = null;
        this.mouseX = 0;
        this.mouseY = 0;
        this.beads = [];
        this.hue = 0;

        this.init();
    }

    init() {
        this.canvas = document.createElement('canvas');
        this.canvas.id = 'elegant-water-beads';
        this.canvas.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            pointer-events: none;
            z-index: 9998;
        `;

        document.body.appendChild(this.canvas);
        this.ctx = this.canvas.getContext('2d');

        this.resize();
        this.bindEvents();
        this.createBeads();
        this.animate();
    }

    resize() {
        this.canvas.width = window.innerWidth;
        this.canvas.height = window.innerHeight;
    }

    bindEvents() {
        document.addEventListener('mousemove', (e) => {
            this.mouseX = e.clientX;
            this.mouseY = e.clientY;
        });

        window.addEventListener('resize', () => this.resize());
    }

    createBeads() {
        const beadCount = 32;
        this.beads = [];

        for (let i = 0; i < beadCount; i++) {
            this.beads.push({
                index: i,
                size: Math.random() * 5 + 3,
                wavePhase: Math.random() * Math.PI * 2,
                waveSpeed: Math.random() * 0.08 + 0.03,
                dripTimer: Math.random() * 100,
                isDripping: false,
                dripProgress: 0
            });
        }
    }

    animate() {
        this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);

        this.hue = (this.hue + 0.5) % 360;
        this.updateBeads();
        this.drawBackground();
        this.drawBeadBorder();

        requestAnimationFrame(() => this.animate());
    }

    updateBeads() {
        this.beads.forEach(bead => {
            bead.wavePhase += bead.waveSpeed;
            bead.dripTimer--;

            if (bead.dripTimer <= 0 && !bead.isDripping) {
                bead.isDripping = true;
                bead.dripProgress = 0;
                bead.dripTimer = Math.random() * 200 + 100;
            }

            if (bead.isDripping) {
                bead.dripProgress += 0.03;
                if (bead.dripProgress >= 1) {
                    bead.isDripping = false;
                }
            }
        });
    }

    drawBackground() {
        const width = 220;
        const height = 140;
        const x = this.mouseX - width / 2;
        const y = this.mouseY - height / 2;

        // 水波纹背景
        this.ctx.save();

        const gradient = this.ctx.createLinearGradient(x, y, x + width, y + height);
        gradient.addColorStop(0, `hsla(${this.hue}, 70%, 60%, 0.2)`);
        gradient.addColorStop(1, `hsla(${(this.hue + 60) % 360}, 70%, 60%, 0.1)`);

        this.ctx.fillStyle = gradient;
        this.ctx.fillRect(x, y, width, height);

        // 内发光效果
        this.ctx.strokeStyle = `hsla(${this.hue}, 60%, 70%, 0.3)`;
        this.ctx.lineWidth = 1;
        this.ctx.strokeRect(x + 2, y + 2, width - 4, height - 4);

        this.ctx.restore();
    }

    drawBeadBorder() {
        const width = 220;
        const height = 140;
        const centerX = this.mouseX;
        const centerY = this.mouseY;

        this.beads.forEach(bead => {
            const angle = (bead.index / this.beads.length) * Math.PI * 2;
            const waveOffset = Math.sin(bead.wavePhase) * 3;

            // 计算边框位置
            let x, y;
            const segments = 4;
            const segmentAngle = Math.PI * 2 / segments;
            const currentSegment = Math.floor(angle / segmentAngle);
            const segmentProgress = (angle % segmentAngle) / segmentAngle;

            switch (currentSegment) {
                case 0: // 上边框
                    x = centerX - width/2 + segmentProgress * width;
                    y = centerY - height/2 - 8 + waveOffset;
                    break;
                case 1: // 右边框
                    x = centerX + width/2 + 8 - waveOffset;
                    y = centerY - height/2 + segmentProgress * height;
                    break;
                case 2: // 下边框
                    x = centerX + width/2 - segmentProgress * width;
                    y = centerY + height/2 + 8 - waveOffset;
                    break;
                case 3: // 左边框
                    x = centerX - width/2 - 8 + waveOffset;
                    y = centerY + height/2 - segmentProgress * height;
                    break;
            }

            this.drawBead(x, y, bead);
        });
    }

    drawBead(x, y, bead) {
        this.ctx.save();

        // 水珠主体
        const gradient = this.ctx.createRadialGradient(
            x - bead.size * 0.2, y - bead.size * 0.2, 0,
            x, y, bead.size
        );

        gradient.addColorStop(0, `hsla(${this.hue}, 100%, 90%, 0.9)`);
        gradient.addColorStop(0.4, `hsla(${this.hue}, 80%, 60%, 0.8)`);
        gradient.addColorStop(1, `hsla(${this.hue}, 60%, 40%, 0.6)`);

        this.ctx.fillStyle = gradient;
        this.ctx.beginPath();
        this.ctx.arc(x, y, bead.size, 0, Math.PI * 2);
        this.ctx.fill();

        // 强烈高光
        this.ctx.fillStyle = 'rgba(255, 255, 255, 0.9)';
        this.ctx.beginPath();
        this.ctx.arc(
            x - bead.size * 0.15,
            y - bead.size * 0.15,
            bead.size * 0.3,
            0, Math.PI * 2
        );
        this.ctx.fill();

        // 反射光
        this.ctx.fillStyle = 'rgba(255, 255, 255, 0.4)';
        this.ctx.beginPath();
        this.ctx.arc(
            x + bead.size * 0.2,
            y + bead.size * 0.2,
            bead.size * 0.2,
            0, Math.PI * 2
        );
        this.ctx.fill();

        // 绘制水滴
        if (bead.isDripping) {
            this.drawElegantDrip(x, y, bead);
        }

        this.ctx.restore();
    }

    drawElegantDrip(x, y, bead) {
        const dripLength = 25;
        const progress = bead.dripProgress;

        this.ctx.save();

        if (progress < 0.4) {
            // 水滴形成
            const formProgress = progress / 0.4;
            const dripSize = bead.size * (0.5 + formProgress * 0.5);

            this.ctx.fillStyle = `hsla(${this.hue}, 80%, 60%, ${0.8 * (1 - formProgress)})`;
            this.ctx.beginPath();
            this.ctx.ellipse(
                x, y + bead.size + dripSize * formProgress,
                dripSize * 0.8, dripSize,
                0, 0, Math.PI * 2
            );
            this.ctx.fill();
        } else {
            // 水滴下落
            const fallProgress = (progress - 0.4) / 0.6;
            const currentY = y + bead.size + dripLength * fallProgress;
            const dropSize = bead.size * (1 - fallProgress * 0.5);

            // 水滴
            this.ctx.fillStyle = `hsla(${this.hue}, 80%, 60%, ${0.8 * (1 - fallProgress)})`;
            this.ctx.beginPath();
            this.ctx.arc(x, currentY, dropSize, 0, Math.PI * 2);
            this.ctx.fill();

            // 连接的水柱
            if (fallProgress < 0.8) {
                this.ctx.strokeStyle = `hsla(${this.hue}, 80%, 60%, ${0.6 * (1 - fallProgress)})`;
                this.ctx.lineWidth = 2;
                this.ctx.beginPath();
                this.ctx.moveTo(x, y + bead.size);
                this.ctx.lineTo(x, currentY - dropSize);
                this.ctx.stroke();
            }
        }

        this.ctx.restore();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    new ElegantWaterBeads();
});