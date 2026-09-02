#!/bin/sh
# 容器入口脚本：恢复默认静态资源 -> 等待 MySQL/Redis 就绪 -> 启动 Waitress
# 注意：本文件必须是 LF 换行（Dockerfile 里已做 CRLF 兜底转换）
set -e

echo "=========================================="
echo " 物理考试系统 容器启动"
echo " 时间: $(date '+%Y-%m-%d %H:%M:%S')  时区: ${TZ:-未设置}"
echo "=========================================="

# ---------- 1) 恢复默认静态资源 ----------
# 命名卷首次挂载会继承镜像内容，但卷已存在时（例如后续新增了题目图片）不会更新，
# 所以从镜像构建阶段备份的 /opt/default_* 里补齐缺失文件（不覆盖用户已上传的同名文件）。
restore_defaults() {
    src="$1"
    dst="$2"
    [ -d "$src" ] || return 0
    mkdir -p "$dst"
    cp -rn "$src/." "$dst/" 2>/dev/null || true
}

restore_defaults /opt/default_images  /app/static/images
restore_defaults /opt/default_avatars /app/static/avatars
mkdir -p /app/logs
echo "[INIT] 默认静态资源已检查/补齐"

# ---------- 2) 等待 MySQL 就绪 ----------
# 仅靠 compose 的 healthcheck 不够可靠：官方 mysql 镜像初始化阶段
# mysqladmin ping 可能提前成功，此时业务库/授权还没建好。这里用真实连接探测。
echo "[WAIT] 正在等待 MySQL (${MYSQL_HOST:-db}:${MYSQL_PORT:-3306}) ..."
python - <<'PYEOF'
import os, sys, time
import mysql.connector

host = os.getenv("MYSQL_HOST", "db")
port = int(os.getenv("MYSQL_PORT", "3306"))
user = os.getenv("MYSQL_USER", "root")
password = os.getenv("MYSQL_PASSWORD", "")
database = os.getenv("MYSQL_DATABASE", "physics_new3")

deadline = time.time() + 180
attempt = 0
while time.time() < deadline:
    attempt += 1
    try:
        conn = mysql.connector.connect(
            host=host, port=port, user=user,
            password=password, database=database,
            connection_timeout=5,
        )
        conn.close()
        print(f"[WAIT] MySQL 就绪（第 {attempt} 次探测）")
        sys.exit(0)
    except Exception as err:
        if attempt % 5 == 1:
            print(f"[WAIT] MySQL 尚未就绪: {err}")
        time.sleep(3)

print("[FATAL] 等待 MySQL 超时（180s），退出")
sys.exit(1)
PYEOF

# ---------- 3) 等待 Redis 就绪 ----------
# Redis 不可用不阻塞启动：extensions.py 会自动降级到内存缓存，
# 但多实例部署时内存缓存不共享，所以这里给出明确告警。
echo "[WAIT] 正在等待 Redis (${REDIS_URL:-redis://redis:6379/0}) ..."
python - <<'PYEOF'
import os, time
import redis

url = os.getenv("REDIS_URL", "redis://redis:6379/0")
deadline = time.time() + 60
attempt = 0
while time.time() < deadline:
    attempt += 1
    try:
        redis.Redis.from_url(url, socket_connect_timeout=3).ping()
        print(f"[WAIT] Redis 就绪（第 {attempt} 次探测）")
        break
    except Exception as err:
        if attempt % 5 == 1:
            print(f"[WAIT] Redis 尚未就绪: {err}")
        time.sleep(2)
else:
    print("[WARN] Redis 等待超时，应用将降级为内存缓存（题目池不跨实例共享）")
PYEOF

# ---------- 4) 启动应用 ----------
echo "[BOOT] 启动 start_server.py (PORT=${PORT:-5000})"
exec python start_server.py
