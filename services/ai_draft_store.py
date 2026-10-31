"""Durable drafts/settings. Tables are created only on an enabled admin request."""
import json
import threading
import time
import uuid

from services.ai_template_compiler import require


class DraftStore:
    def __init__(self, connect):
        self.connect = connect
        self.initialized = False
        self.lock = threading.Lock()

    def ensure(self):
        if self.initialized:
            return
        with self.lock:
            if self.initialized:
                return
            conn = self.connect()
            require(conn is not None, '数据库暂不可用')
            cursor = conn.cursor()
            try:
                cursor.execute('''CREATE TABLE IF NOT EXISTS ai_question_drafts (
                    id VARCHAR(32) PRIMARY KEY, paper_id INT NOT NULL,
                    status VARCHAR(24) NOT NULL, message TEXT NOT NULL,
                    data MEDIUMTEXT NOT NULL, template_id INT NULL,
                    created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
                    INDEX ai_drafts_paper (paper_id, created_at)
                )''')
                cursor.execute('''CREATE TABLE IF NOT EXISTS ai_question_settings (
                    id INT PRIMARY KEY, rules TEXT NOT NULL
                )''')
                conn.commit()
                self.initialized = True
            finally:
                cursor.close()
                conn.close()

    @staticmethod
    def decode(row):
        if row:
            row['data'] = json.loads(row['data'])
        return row

    def create(self, paper_id, data):
        draft_id, now = uuid.uuid4().hex, time.time()
        self.execute('''INSERT INTO ai_question_drafts
            (id,paper_id,status,message,data,created_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s)''',
            (draft_id, paper_id, 'queued', '等待处理', json.dumps(data, ensure_ascii=False), now, now))
        return draft_id

    def execute(self, sql, args):
        conn = self.connect()
        require(conn is not None, '数据库暂不可用')
        cursor = conn.cursor()
        try:
            cursor.execute(sql, args)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def update(self, draft_id, status, message, data=None):
        fields = 'status=%s,message=%s,updated_at=%s'
        args = [status, message[:1500], time.time()]
        if data is not None:
            fields += ',data=%s'
            args.append(json.dumps(data, ensure_ascii=False, allow_nan=False))
        args.append(draft_id)
        self.execute('UPDATE ai_question_drafts SET ' + fields + " WHERE id=%s AND status IN ('queued','generating','validating')", args)

    def list(self, paper_id=None, draft_id=None):
        # A crashed/restarted worker cannot leave a permanently spinning draft.
        self.execute("UPDATE ai_question_drafts SET status='failed',message=%s WHERE status IN ('queued','generating','validating') AND updated_at<%s",
                     ('任务已超时或服务重启，请重新生成；原输入仍保留。', time.time() - 420))
        conn = self.connect()
        require(conn is not None, '数据库暂不可用')
        cursor = conn.cursor(dictionary=True)
        try:
            if draft_id:
                cursor.execute('SELECT * FROM ai_question_drafts WHERE id=%s', (draft_id,))
            else:
                cursor.execute('SELECT * FROM ai_question_drafts WHERE paper_id=%s ORDER BY created_at DESC LIMIT 50', (paper_id,))
            return [self.decode(row) for row in cursor.fetchall()]
        finally:
            cursor.close()
            conn.close()

    def rules(self, default, value=None):
        conn = self.connect()
        require(conn is not None, '数据库暂不可用')
        cursor = conn.cursor(dictionary=True)
        try:
            if value is not None:
                cursor.execute('INSERT INTO ai_question_settings (id,rules) VALUES (1,%s) ON DUPLICATE KEY UPDATE rules=VALUES(rules)', (value,))
                conn.commit()
                return value
            cursor.execute('SELECT rules FROM ai_question_settings WHERE id=1')
            row = cursor.fetchone()
            return row['rules'] if row else default
        finally:
            cursor.close()
            conn.close()
