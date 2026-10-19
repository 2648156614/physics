"""Exercise arrangement edits against a transactional SQL database without production data."""
from datetime import datetime
from functools import wraps
import importlib.util
import ast
import io
from pathlib import Path
import sqlite3
import types
import unittest
from unittest.mock import Mock

from flask import Flask, flash, redirect, request, session, url_for
from openpyxl import Workbook
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, StrictUndefined

from services.seb_exam_service import calculate_config_key_request_hash, validate_exam_config_request


class Cursor:
    def __init__(self, db):
        self.cursor = db.cursor()

    def sql(self, sql):
        sql = sql.replace('%s', '?').replace(' FOR UPDATE', '').replace('BINARY ', '')
        sql = sql.replace('INSERT IGNORE', 'INSERT OR IGNORE')
        return sql.replace('ON DUPLICATE KEY UPDATE is_active = TRUE',
                           'ON CONFLICT(exam_id, user_id) DO UPDATE SET is_active = TRUE')

    def execute(self, sql, params=()):
        self.cursor.execute(self.sql(sql), params)

    @property
    def lastrowid(self):
        return self.cursor.lastrowid

    def executemany(self, sql, params):
        self.cursor.executemany(self.sql(sql), params)

    def row(self, row):
        if row is None:
            return None
        row = dict(row)
        for field in ('start_time', 'end_time', 'batch_start_time', 'batch_end_time'):
            if isinstance(row.get(field), str):
                row[field] = datetime.fromisoformat(row[field])
        return row

    def fetchone(self):
        return self.row(self.cursor.fetchone())

    def fetchall(self):
        return [self.row(row) for row in self.cursor.fetchall()]

    def close(self):
        self.cursor.close()


class Connection:
    def __init__(self, db):
        self.db = db

    def cursor(self, **kwargs):
        return Cursor(self.db)

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()

    def close(self):
        pass


class ArrangementRouteTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE exams(id INTEGER PRIMARY KEY, name TEXT, paper_id INT, question_count INT,
                exam_type TEXT, start_time TEXT, end_time TEXT, status TEXT, seb_required INT,
                seb_config_key TEXT, seb_previous_config_keys TEXT, seb_quit_token TEXT);
            CREATE TABLE exam_batches(id INTEGER PRIMARY KEY, exam_id INT, name TEXT, start_time TEXT,
                end_time TEXT, status TEXT, is_default INT);
            CREATE TABLE users(id INT PRIMARY KEY, username TEXT, name TEXT, class_name TEXT,
                major TEXT, teacher_name TEXT);
            CREATE TABLE exam_papers(id INT PRIMARY KEY, name TEXT, description TEXT, is_enabled INT);
            INSERT INTO exam_papers VALUES(10,'paper','',1);
            CREATE TABLE exam_assignments(exam_id INT, assign_type TEXT, assign_value TEXT,
                UNIQUE(exam_id, assign_type, assign_value));
            CREATE TABLE exam_batch_assignments(batch_id INT, assign_type TEXT, assign_value TEXT);
            CREATE TABLE exam_batch_students(exam_id INT, batch_id INT, user_id INT,
                is_active INT DEFAULT 1, UNIQUE(exam_id,user_id));
            CREATE TABLE exam_user_questions(exam_id INT, batch_id INT, user_id INT, template_id INT);
            CREATE TABLE user_responses(exam_id INT, batch_id INT, user_id INT, answer TEXT);
            INSERT INTO exam_batches VALUES(7,9,'default','2026-10-19 09:00','2026-10-19 12:00','active',1);
            INSERT INTO exam_batches VALUES(8,9,'second','2026-10-19 09:00','2026-10-19 12:00','active',0);
            INSERT INTO users VALUES(1,'1001','one','A','P','C'),(2,'1002','two','A','P','C'),(3,'1003','three','B','P','C');
            INSERT INTO exam_assignments VALUES(9,'class','A');
            INSERT INTO exam_batch_assignments VALUES(7,'class','A');
            INSERT INTO exam_batch_students VALUES(9,7,1,1);
            INSERT INTO exam_user_questions VALUES(9,7,1,101);
            INSERT INTO user_responses VALUES(9,7,1,'saved-answer');
        ''')
        self.db.execute('INSERT INTO exams VALUES(9,?,?,?,?,?,?,?,?,?,?,?)',
                        ('exam', 10, 2, 'normal', '2026-10-19 09:00', '2026-10-19 12:00', 'published', 1, 'a'*64, None, 'token'))
        self.db.commit()
        self.app = Flask(__name__)
        self.app.secret_key = 'test-only'

        def login_required(view=None, **kwargs):
            if view is None:
                return login_required
            @wraps(view)
            def wrapped(*args, **kw):
                return view(*args, **kw)
            return wrapped

        def get_exam(exam_id):
            cur = Cursor(self.db)
            cur.execute('SELECT * FROM exams WHERE id = %s', (exam_id,))
            return cur.fetchone()

        self.deps = {
            'login_required': login_required, 'request': request, 'session': session,
            'flash': flash, 'redirect': redirect, 'url_for': url_for,
            'get_db_connection': lambda: Connection(self.db), 'get_exam_by_id': get_exam,
            'get_problem_templates_by_paper': Mock(return_value=[{'id': 101}, {'id': 102}, {'id': 103}]),
            'parse_exam_time': lambda value: datetime.fromisoformat(value) if value else None,
            'clear_exam_metadata_cache': Mock(), 'ensure_exam_paper_enabled': Mock(return_value=False),
            'allowed_excel_file': lambda name: name.endswith('.xlsx'),
            'replace_exam_question_pool': Mock(return_value=3),
            'get_exam_question_pool_count': Mock(return_value=2),
            'mysql': types.SimpleNamespace(connector=types.SimpleNamespace(Error=sqlite3.DatabaseError)),
        }
        spec = importlib.util.spec_from_file_location('admin_arrangement_under_test', Path(__file__).parents[1] / 'routes/admin.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.app.register_blueprint(module.create_admin_blueprint(self.deps))
        self.app.add_url_rule('/dashboard', endpoint='dashboard', view_func=lambda: 'dashboard')
        self.client = self.app.test_client()
        with self.client.session_transaction() as state:
            state['username'] = 'admin'

    def tearDown(self):
        self.db.close()

    def exam(self):
        return self.deps['get_exam_by_id'](9)

    def post_exam(self, **changes):
        values = dict(name='renamed', paper_id='10', question_count='2', exam_type='normal',
                      assign_type='class', assign_value='A', start_time='2026-10-19T08:00',
                      end_time='2026-10-19T13:00', seb_required='on', seb_config_key='a'*64)
        values.update(changes)
        return self.client.post('/admin/exams/9/update', data=values)

    def active(self):
        return [row[0] for row in self.db.execute('SELECT user_id FROM exam_batch_students WHERE is_active=1 ORDER BY user_id')]

    def assert_history(self):
        self.assertEqual(tuple(self.db.execute('SELECT * FROM user_responses').fetchone()), (9,7,1,'saved-answer'))
        self.assertEqual(tuple(self.db.execute('SELECT * FROM exam_user_questions').fetchone()), (9,7,1,101))

    def test_started_exam_allows_name_time_key_and_new_class_members(self):
        self.assertEqual(self.post_exam(seb_config_key='b'*64).status_code, 302)
        exam = self.exam()
        self.assertEqual(exam['name'], 'renamed')
        self.assertEqual(exam['end_time'].hour, 13)
        self.assertEqual(self.active(), [1,2])
        url = 'https://exam.test/problem_ajax/1'
        for key in ['a'*64, 'b'*64]:
            self.assertTrue(validate_exam_config_request(exam, url, calculate_config_key_request_hash(url,key))[0])
        self.assert_history()

    def test_revoking_old_keys_rejects_old_configuration(self):
        self.post_exam(seb_config_key='b'*64)
        self.post_exam(seb_config_key='b'*64, retire_previous_seb_keys='on')
        url = 'https://exam.test/problem_ajax/1'
        self.assertFalse(validate_exam_config_request(self.exam(),url,calculate_config_key_request_hash(url,'a'*64))[0])

    def test_roster_replacement_retains_history_and_can_restore_members(self):
        self.post_exam(assign_value='B')
        self.assertEqual(self.active(), [3])
        self.assertEqual(self.db.execute('SELECT is_active FROM exam_batch_students WHERE user_id=1').fetchone()[0], 0)
        self.assert_history()

    def test_removed_students_lose_access_but_readding_restores_original_paper(self):
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'get_user_available_exams')
        namespace = dict(self.deps, datetime=datetime)
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), namespace)
        available = namespace['get_user_available_exams']
        self.assertEqual(len(available(1)),1)
        self.post_exam(assign_value='B')
        self.assertEqual(available(1),[])
        self.assertEqual(len(available(3)),1)
        self.post_exam(assign_value='A')
        self.assertEqual(self.active(), [1,2])
        self.assertEqual(len(available(1)),1)
        self.assert_history()

    def test_shortening_requires_acknowledgement_and_rolls_back_other_edits(self):
        self.post_exam(end_time='2026-10-19T11:00')
        self.assertEqual(self.exam()['name'], 'exam')
        self.post_exam(end_time='2026-10-19T11:00', confirm_time_change='on')
        self.assertEqual(self.exam()['end_time'].hour, 11)
        self.assert_history()

    def test_question_count_and_paper_stay_locked(self):
        for values in [{'question_count':'3'}, {'paper_id':'11'}]:
            self.post_exam(**values)
            self.assertEqual(self.exam()['paper_id'],10)
            self.assertEqual(self.exam()['question_count'],2)
        self.deps['replace_exam_question_pool'].assert_not_called()
        self.assert_history()

    def test_archived_templates_do_not_block_unrelated_arrangement_edits(self):
        self.deps['get_problem_templates_by_paper'].return_value = [{'id':101}]
        self.post_exam()
        self.assertEqual(self.exam()['name'],'renamed')
        self.assert_history()

    def test_legacy_all_questions_count_uses_frozen_pool(self):
        self.db.execute('UPDATE exams SET question_count=NULL')
        self.db.commit()
        self.post_exam()
        self.assertEqual(self.exam()['name'],'renamed')
        self.assertIsNone(self.exam()['question_count'])
        self.assert_history()

    def test_new_exam_creation_still_works(self):
        response = self.client.post('/admin/exams/create',data=dict(name='new exam',paper_id='10',question_count='2',
                                   exam_type='normal',assign_type='class',assign_value='A'))
        self.assertEqual(response.status_code,302)
        self.assertIsNotNone(self.db.execute("SELECT id FROM exams WHERE name='new exam'").fetchone())

    def test_admin_templates_render_edit_controls_and_preserve_history_labels(self):
        env = Environment(loader=ChoiceLoader([
            DictLoader({'base.html':'{% block title %}{% endblock %}{% block content %}{% endblock %}'}),
            FileSystemLoader(Path(__file__).parents[1] / 'templates'),
        ]), undefined=StrictUndefined, autoescape=True)
        env.globals['url_for'] = lambda endpoint, **kw: '/'+endpoint
        exam = self.exam()
        exam.update(editable_question_count=2, paper_name='paper', batch_count=2,student_count=1)
        context = dict(editing_exam=exam,exams=[exam],exam_papers=[dict(id=10,template_count=3,is_enabled=True,name='paper')],
                       class_options=['A'],major_options=['P'],course_options=['C'],retake_candidates=[],
                       editing_assignments=[],editing_assign_type='class',editing_assign_values=['A'],
                       selected_source_exam_id=None,previous_seb_key_count=1)
        html = env.get_template('admin_exams.html').render(**context)
        for control in ['student_import_mode','retire_previous_seb_keys','confirm_time_change','exam.seb_test']:
            self.assertIn(control,html)
        batch = dict(id=7,name='default',start_time=None,end_time=None,status='active',student_count=1)
        from services.exam_batch_service import EXAM_STUDENT_STATUS_LABELS
        context = dict(exam=exam,batches=[batch],selected_batch=batch,selected_batch_id=7,total_students=1,
                       total_problems=2,status_counts={key:0 for key in EXAM_STUDENT_STATUS_LABELS},
                       status_labels=EXAM_STUDENT_STATUS_LABELS,selected_status='',selected_class_name='',
                       selected_major='',selected_course='',batch_class_options=[],batch_major_options=[],
                       batch_course_options=[],class_options=['A'],major_options=['P'],course_options=['C'],students=[])
        html = env.get_template('admin_exam_detail.html').render(**context)
        self.assertIn('admin.admin_update_exam_batch',html)
        self.assertIn('已移出名单',html)

    def test_cross_batch_conflict_rolls_back_entire_edit(self):
        self.db.execute('INSERT INTO exam_batch_students VALUES(9,8,3,1)')
        self.db.commit()
        self.post_exam(assign_value='B')
        self.assertEqual(self.exam()['name'], 'exam')
        self.assertEqual(self.active(), [1,3])
        self.assert_history()

    def spreadsheet(self):
        wb = Workbook()
        wb.active.append(['学号','姓名'])
        wb.active.append(['1003','three'])
        content = io.BytesIO()
        wb.save(content)
        content.seek(0)
        return (content, 'students.xlsx')

    def test_excel_append_and_replace_are_distinct(self):
        self.post_exam(assign_type='student', student_file=self.spreadsheet())
        self.assertEqual(self.active(),[1,3])
        self.post_exam(assign_type='student', student_file=self.spreadsheet(), student_import_mode='replace')
        self.assertEqual(self.active(),[3])
        self.assert_history()

    def test_each_batch_can_edit_times_and_append_students(self):
        response = self.client.post('/admin/exams/9/batches/8/update', data={
            'start_time':'2026-10-19T08:00', 'end_time':'2026-10-19T14:00',
            'assign_type':'class', 'assign_value':'B',
        })
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.db.execute('SELECT end_time FROM exam_batches WHERE id=8').fetchone()[0], '2026-10-19 14:00:00')
        self.assertEqual(self.exam()['end_time'].hour,12)
        self.assertEqual(self.active(),[1,3])
        self.assert_history()

    def test_archived_exam_and_non_admin_remain_protected(self):
        self.db.execute("UPDATE exams SET status='archived'")
        self.db.commit()
        self.post_exam()
        self.assertEqual(self.exam()['name'],'exam')
        with self.client.session_transaction() as state:
            state['username']='student'
        self.assertEqual(self.post_exam().headers['Location'],'/dashboard')


if __name__ == '__main__':
    unittest.main()
