"""Persistent shared SEB configuration, independent of releases and Redis caches."""
import uuid

from services.seb_exam_service import normalize_config_key, rotate_config_keys


def load_settings(connect):
    conn = connect()
    if not conn:
        raise RuntimeError('无法读取统一 SEB 配置，请稍后重试。')
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute('SELECT * FROM seb_settings WHERE id = 1')
        return cursor.fetchone() or {}
    finally:
        cursor.close()
        conn.close()


def save_settings(connect, raw_key, retire_previous=False):
    key = normalize_config_key(raw_key)
    if not key:
        raise ValueError('SEB Config Key 必须是 64 位十六进制字符。')
    conn = connect()
    if not conn:
        raise RuntimeError('数据库暂时不可用，配置尚未保存。')
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute('SELECT * FROM seb_settings WHERE id = 1 FOR UPDATE')
        old = cursor.fetchone() or {}
        if not old:
            raise RuntimeError('统一 SEB 配置尚未初始化，请完成服务器更新后重试。')
        retained = rotate_config_keys(old, key, retire_previous)
        cursor.execute('UPDATE seb_settings SET seb_config_key = %s, seb_previous_config_keys = %s WHERE id = 1',
                       (key, retained))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def ensure_schema(cursor):
    cursor.execute("""CREATE TABLE IF NOT EXISTS seb_settings (
        id INT PRIMARY KEY,
        seb_config_key CHAR(64) NULL,
        seb_previous_config_keys TEXT NULL,
        seb_quit_token CHAR(32) NOT NULL
    )""")
    cursor.execute('INSERT IGNORE INTO seb_settings (id, seb_quit_token) VALUES (1, %s)', (uuid.uuid4().hex,))
    cursor.execute("SHOW COLUMNS FROM exams LIKE 'seb_config_mode'")
    if not cursor.fetchone():
        cursor.execute("ALTER TABLE exams ADD COLUMN seb_config_mode VARCHAR(16) NOT NULL DEFAULT 'exam'")
