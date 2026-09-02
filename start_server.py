import os
import sys
import logging
from datetime import datetime
from app import (
    app,
    initialize_database,
    ensure_user_columns,
    ensure_performance_indexes,
    create_admin_user,
    repair_database,
    prewarm_pools,
    get_upload_folder_path,
)


# 配置日志
def setup_logging(port: int):
    os.makedirs('logs', exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(f'logs/waitress_{port}.log', encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )


def get_port() -> int:
    """从环境变量读取端口，默认 5000"""
    raw = os.environ.get("PORT", "5000").strip()
    try:
        port = int(raw)
        if not (1 <= port <= 65535):
            raise ValueError("port out of range")
        return port
    except Exception:
        print(f"❌ 环境变量 PORT 无效: {raw}，请设置为 1~65535 的整数")
        sys.exit(1)


def get_int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
        if value < minimum:
            raise ValueError(f"{name} must be >= {minimum}")
        return value
    except Exception:
        print(f"❌ 环境变量 {name} 无效: {raw}，已回退为默认值 {default}")
        return default


if __name__ == '__main__':
    port = get_port()
    waitress_threads = get_int_env("WAITRESS_THREADS", 32, minimum=1)
    waitress_connection_limit = get_int_env("WAITRESS_CONNECTION_LIMIT", 4000, minimum=1)
    waitress_channel_timeout = get_int_env("WAITRESS_CHANNEL_TIMEOUT", 120, minimum=10)
    print("🚀 启动物理考试系统服务器...")

    # 设置日志（不同端口不同日志文件）
    setup_logging(port)

    # 确保目录存在
    os.makedirs(get_upload_folder_path(), exist_ok=True)

    try:
        # 初始化数据库（注意：多实例同时启动时可能会同时跑一遍）
        print("📊 初始化数据库...")
        initialize_database()
        ensure_user_columns()
        create_admin_user()
        repair_database()
        # 必须放在 repair_database() 之后：索引依赖 exam_id / switch_count 等后补字段。
        # 原先只有 app.py 的 __main__ 里调用了它，而容器实际入口是本文件，
        # 导致 9 个高并发关键索引在 Docker 部署下从未创建过。
        ensure_performance_indexes()
        if os.environ.get("PREWARM_ON_START", "0").lower() in ("1", "true", "yes", "on"):
            print("[PREWARM] Prewarming enabled problem pools...")
            prewarm_pools()

        # 生产环境使用 Waitress
        from waitress import serve

        print("🎯 服务器配置信息:")
        print(f"   - 地址: http://0.0.0.0:{port}")
        print(f"   - 线程数: {waitress_threads}")
        print(f"   - 最大连接: {waitress_connection_limit}")
        print(f"   - channel_timeout: {waitress_channel_timeout}")
        print(f"   - 启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 50)

        # 优化后的生产配置
        serve(
            app,
            host='0.0.0.0',
            port=port,
            threads=waitress_threads,
            connection_limit=waitress_connection_limit,
            asyncore_use_poll=True,
            channel_timeout=waitress_channel_timeout,
            ident=f"Physics Exam System :{port}"
        )

    except Exception as e:
        logging.error(f"服务器启动失败: {e}")
        print(f"❌ 服务器启动失败: {e}")
        sys.exit(1)
