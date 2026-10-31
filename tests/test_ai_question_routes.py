import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from flask import Flask
from jinja2 import ChoiceLoader, DictLoader

from ai_question_example import example
from routes.ai_questions import create_ai_question_blueprint
from services.ai_compiled_runtime import generate_compiled


class Cursor:
    def __init__(self, conn): self.cur = conn.cursor()
    def execute(self, sql, args=()):
        sql = sql.replace('%s','?').replace(' FOR UPDATE','')
        sql = re.sub(r',\s*INDEX ai_drafts_paper \(paper_id, created_at\)', '', sql)
        sql = sql.replace('ON DUPLICATE KEY UPDATE rules=VALUES(rules)', 'ON CONFLICT(id) DO UPDATE SET rules=excluded.rules')
        self.cur.execute(sql,args)
    def fetchone(self):
        row = self.cur.fetchone(); return dict(row) if row else None
    def fetchall(self): return [dict(row) for row in self.cur.fetchall()]
    @property
    def lastrowid(self): return self.cur.lastrowid
    def close(self): self.cur.close()


class Connection:
    def __init__(self, path):
        self.conn = sqlite3.connect(path,timeout=10)
        self.conn.row_factory = sqlite3.Row
    def cursor(self, **kwargs): return Cursor(self.conn)
    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()
    def close(self): self.conn.close()


class Immediate:
    def submit(self, fn, *args): fn(*args)


class AIRouteTests(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(__file__).resolve().parent.parent / 'outputs' / 'ai-tests'
        self.test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.test_root)
        self.path = Path(self.temp.name)/'test.sqlite'
        db = sqlite3.connect(self.path)
        db.executescript('''CREATE TABLE exam_papers(id INTEGER PRIMARY KEY,name TEXT);
            INSERT INTO exam_papers VALUES(1,'测试题库');
            CREATE TABLE exams(id INTEGER PRIMARY KEY,paper_id INT,status TEXT);
            CREATE TABLE exam_user_questions(exam_id INT);
            CREATE TABLE user_responses(exam_id INT);
            CREATE TABLE problem_templates(id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_name TEXT,problem_text TEXT,variables TEXT,solution_formula TEXT,
                answer_count INT,answer_units TEXT,difficulty TEXT,image_filename TEXT,
                paper_id INT,knowledge_point TEXT,generation_strategy TEXT);''')
        db.close()
        root = Path(__file__).resolve().parent.parent
        self.app = Flask(__name__,template_folder=str(root/'templates'),static_folder=str(root/'static'))
        self.app.secret_key = 'test-only'
        self.app.jinja_loader = ChoiceLoader([DictLoader({'base.html':'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
            <link rel="stylesheet" href="/static/vendor/bootstrap-5.1.3/css/bootstrap.min.css">
            {% block content %}{% endblock %}{% block scripts %}{% endblock %}'''}),self.app.jinja_loader])
        self.deps = {'get_db_connection':lambda:Connection(self.path),
            'login_required':lambda f=None, **kwargs: f if f else (lambda view:view),
            'ai_source_folder':str(Path(self.temp.name)/'private_sources'),
            'get_exam_paper_by_id':lambda paper_id: {'id':1,'name':'测试题库'} if paper_id == 1 else None,
            'get_upload_folder_path':lambda:self.temp.name,
            'build_problem_image_html':lambda filename,title:f'<img src="/problem_image/{filename}" alt="示意图">',
            'invalidate_exam_paper_cache':Mock(), 'schedule_exam_paper_prewarm':Mock()}
        self.app.register_blueprint(create_ai_question_blueprint(self.deps))
        self.app.add_url_rule('/admin/manage_problems', endpoint='admin_manage_problems',view_func=lambda:'题库')
        self.client = self.app.test_client()
        self.env = patch.dict('os.environ', {'AI_AUTHORING_ENABLED':'true','SECRET_KEY':'test-only'})
        self.env.start()
        self.exec = patch('routes.ai_questions.executor',Immediate()); self.exec.start()
        with self.client.session_transaction() as state:
            state.update(username='admin',ai_csrf='test-csrf')

    def tearDown(self):
        self.exec.stop(); self.env.stop()
        self.assertTrue(Path(self.temp.name).resolve().is_relative_to(self.test_root.resolve()))
        self.temp.cleanup()

    def create(self, **kwargs):
        data = {'paper_id':'1','manual':json.dumps(example()),'difficulty':'medium'}; data.update(kwargs)
        response = self.client.post('/admin/ai-questions/drafts',data=data,headers={'X-AI-CSRF':'test-csrf'})
        self.assertEqual(response.status_code,202,response.get_data(as_text=True))
        return self.client.get('/admin/ai-questions/drafts/'+response.json['id']).json

    def import_draft(self, row, **kwargs):
        body = {'artifact_hash':row['artifact_hash'],'teacher_confirmed':True}; body.update(kwargs)
        return self.client.post('/admin/ai-questions/drafts/'+row['id']+'/import',json=body,headers={'X-AI-CSRF':'test-csrf'})

    def test_admin_page_and_manual_compile_import_and_student_runtime(self):
        self.assertEqual(self.client.get('/admin/ai-questions?paper_id=1').status_code,200)
        row = self.create(); self.assertEqual(row['status'],'ready',row['message'])
        response = self.import_draft(row); self.assertEqual(response.status_code,200,response.json)
        conn = Connection(self.path); cur = conn.cursor(); cur.execute('SELECT * FROM problem_templates')
        template = cur.fetchone(); cur.close(); conn.close()
        problem = generate_compiled(template,json.loads(template['generation_strategy']))
        self.assertNotIn('{{',problem['problem_text']); self.assertNotIn('q',problem['var_values'])
        self.deps['schedule_exam_paper_prewarm'].assert_called_once_with(1)

    def test_duplicate_import_returns_same_template_id(self):
        row = self.create(); a = self.import_draft(row); b = self.import_draft(row)
        self.assertEqual(a.json['template_id'],b.json['template_id']); self.assertTrue(b.json['already_imported'])

    def test_preview_hash_and_teacher_confirmation_required(self):
        row = self.create()
        self.assertEqual(self.import_draft(row,artifact_hash='stale').status_code,400)
        self.assertEqual(self.import_draft(row,teacher_confirmed=False).status_code,400)

    def test_started_or_archived_exam_blocks_import_without_writing(self):
        row = self.create()
        db = sqlite3.connect(self.path); db.execute("INSERT INTO exams VALUES(9,1,'published')")
        db.execute('INSERT INTO exam_user_questions VALUES(9)'); db.commit(); db.close()
        self.assertEqual(self.import_draft(row).status_code,400)
        db = sqlite3.connect(self.path); self.assertEqual(db.execute('SELECT COUNT(*) FROM problem_templates').fetchone()[0],0); db.close()

    def test_permissions_csrf_and_feature_switch(self):
        self.assertEqual(self.client.post('/admin/ai-questions/settings',json={'rules':'x'}).status_code,403)
        with self.client.session_transaction() as state: state['username']='student'
        self.assertEqual(self.client.get('/admin/ai-questions?paper_id=1').status_code,403)
        with self.client.session_transaction() as state: state['username']='admin'
        with patch.dict('os.environ',{'AI_AUTHORING_ENABLED':'false'}):
            self.assertEqual(self.client.get('/admin/ai-questions?paper_id=1').status_code,404)

    def test_new_revision_preserves_old_draft_and_revalidates(self):
        original = self.create(); edited = example(); edited['template']['name']='第二版'
        new = self.create(parent_id=original['id'],manual=json.dumps(edited))
        self.assertNotEqual(original['id'],new['id']); self.assertEqual(new['parent_id'],original['id'])
        self.assertEqual(self.client.get('/admin/ai-questions/drafts/'+original['id']).json['template']['name'],'圆周运动的总加速度')

    def test_invalid_template_never_becomes_importable(self):
        invalid = example(); invalid['template']['formula']='__import__("os")'
        row = self.create(manual=json.dumps(invalid)); self.assertEqual(row['status'],'failed')
        self.assertEqual(self.import_draft(row).status_code,400)

    def test_rules_persist_and_are_not_taken_from_model(self):
        response = self.client.post('/admin/ai-questions/settings',json={'rules':'保留考点'},headers={'X-AI-CSRF':'test-csrf'})
        self.assertEqual(response.status_code,200)
        self.assertIn('保留考点',self.client.get('/admin/ai-questions?paper_id=1').get_data(as_text=True))

    def test_restart_or_timeout_preserves_inputs_and_stops_spinner(self):
        row = self.create()
        db = sqlite3.connect(self.path)
        db.execute("UPDATE ai_question_drafts SET status='generating',updated_at=? WHERE id=?",(time.time()-500,row['id']))
        db.commit(); db.close()
        reply = self.client.get('/admin/ai-questions/drafts/'+row['id']).json
        self.assertEqual(reply['status'],'failed'); self.assertTrue(reply['source'] is not None)

    def test_raw_screenshot_never_enters_student_template(self):
        row = self.create(image=(io.BytesIO(b'\x89PNG\r\n\x1a\n' + b'x'*30),'source.png'))
        self.assertFalse(row['diagram']); self.assertEqual(row['template']['image_filename'],'')
        image = self.client.get('/admin/ai-questions/drafts/'+row['id']+'/image/image')
        self.assertEqual(image.status_code,200); image.close()
        self.assertEqual(self.import_draft(row).status_code,200)

    def test_svg_upload_and_invalid_answer_limits_are_rejected(self):
        response = self.client.post('/admin/ai-questions/drafts',data={'paper_id':'1','manual':json.dumps(example()),
                                   'image':(io.BytesIO(b'<svg onload="alert(1)"></svg>'),'x.svg')},headers={'X-AI-CSRF':'test-csrf'})
        self.assertEqual(response.status_code,400)
        row = self.create(answer_limit='1'); self.assertEqual(row['status'],'ready')


if __name__=='__main__': unittest.main()
