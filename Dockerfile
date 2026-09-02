# 使用官方轻量级 Python 3.9 镜像
FROM python:3.9-slim

# ---------- 环境变量 ----------
# PYTHONUNBUFFERED=1 必须加：本项目大量使用 print() 输出诊断信息，
# 不关缓冲会导致 docker compose logs 长时间看不到任何输出，排错时非常痛苦。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    TZ=Asia/Shanghai

# ---------- 时区 ----------
# slim 镜像默认没有完整 tzdata，缺失时 TZ 变量不生效，datetime.now() 会拿到 UTC，
# 导致考试的开始/结束时间整体偏差 8 小时。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 设置容器里的工作目录为 /app
WORKDIR /app

# 先把依赖清单复制到容器里（单独一层，代码改动时不会重装依赖）
COPY requirements.txt .

# 使用国内清华源安装依赖
RUN pip install --no-cache-dir -r requirements.txt

# 把当前目录下的所有代码复制到容器里（.dockerignore 已排除 .env / __pycache__ / logs）
COPY . .

# 备份一份默认静态资源（题目图片、默认头像），供挂载卷后由 entrypoint 恢复
RUN mkdir -p /opt/default_images /opt/default_avatars \
    && ([ -d /app/static/images ] && cp -r /app/static/images/. /opt/default_images/ || true) \
    && ([ -d /app/static/avatars ] && cp -r /app/static/avatars/. /opt/default_avatars/ || true)

# 入口脚本：等 MySQL/Redis 就绪后再启动，并恢复默认静态资源
# sed 兜底：在 Windows 上编辑过的 .sh 若带 CRLF，Linux 下会报 exec format error
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh \
    && chmod +x /app/docker-entrypoint.sh

# 暴露 5000 端口（供容器内部/反向代理使用）
EXPOSE 5000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
