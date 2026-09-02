import logging
import threading
import time

import mysql.connector
from mysql.connector import pooling
from mysql.connector.errors import PoolError

from config import (
    DB_CONFIG,
    DB_POOL_NAME,
    DB_POOL_RESET_SESSION,
    DB_POOL_SIZE,
    MYSQL_CONNECTOR_POOL_MAX_SIZE,
)

logger = logging.getLogger(__name__)
db_pool = None

# 连接池初始化加锁：Waitress 多线程同时收到首个请求时，原来的写法可能并发创建
# 多个连接池，导致实际打开的 MySQL 连接数成倍膨胀。
_pool_lock = threading.Lock()

# 取连接重试：池被打满时 get_connection() 会立即抛 PoolError。
# 原实现直接返回 None，而绝大多数调用方紧接着执行 conn.cursor()，
# 会抛 AttributeError 并变成 500。短暂重试可以吸收瞬时并发峰值。
POOL_ACQUIRE_RETRIES = 5
POOL_ACQUIRE_DELAY_SECONDS = 0.15


def _get_pool():
    """惰性初始化连接池（双重检查加锁）。"""
    global db_pool
    if db_pool is not None:
        return db_pool

    with _pool_lock:
        if db_pool is None:
            normalized_pool_size = max(1, min(DB_POOL_SIZE, MYSQL_CONNECTOR_POOL_MAX_SIZE))
            if normalized_pool_size != DB_POOL_SIZE:
                logger.warning(
                    "DB_POOL_SIZE=%s 超出 mysql-connector 支持范围，已自动调整为 %s",
                    DB_POOL_SIZE,
                    normalized_pool_size,
                )

            db_pool = pooling.MySQLConnectionPool(
                pool_name=DB_POOL_NAME,
                pool_size=normalized_pool_size,
                pool_reset_session=DB_POOL_RESET_SESSION,
                **DB_CONFIG
            )
            logger.info("MySQL连接池初始化成功: %s, 大小=%s", DB_POOL_NAME, normalized_pool_size)

    return db_pool


def get_db_connection():
    """获取数据库连接。连接池打满时短暂重试，失败才返回 None。"""
    try:
        pool = _get_pool()
    except mysql.connector.Error as err:
        logger.error("数据库连接池初始化失败：%s", err)
        return None

    last_error = None

    for attempt in range(1, POOL_ACQUIRE_RETRIES + 1):
        try:
            conn = pool.get_connection()
        except PoolError as err:
            # 池已耗尽：等一小会儿让其他线程归还连接
            last_error = err
            if attempt < POOL_ACQUIRE_RETRIES:
                time.sleep(POOL_ACQUIRE_DELAY_SECONDS)
                continue
            break
        except mysql.connector.Error as err:
            logger.error("数据库连接失败：%s", err)
            return None

        try:
            conn.ping(reconnect=True, attempts=1, delay=0)
            return conn
        except mysql.connector.Error as err:
            # 连接已失效，归还后重试
            last_error = err
            try:
                conn.close()
            except Exception:
                pass
            if attempt < POOL_ACQUIRE_RETRIES:
                time.sleep(POOL_ACQUIRE_DELAY_SECONDS)
                continue

    logger.error(
        "获取数据库连接失败（已重试 %s 次，池大小=%s）：%s",
        POOL_ACQUIRE_RETRIES,
        min(DB_POOL_SIZE, MYSQL_CONNECTOR_POOL_MAX_SIZE),
        last_error,
    )
    return None
