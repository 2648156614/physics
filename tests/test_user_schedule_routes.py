import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, StrictUndefined
from flask import request

import test_exam_arrangement_routes as fixture


class UserScheduleRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ArrangementRouteTests()
        self.fixture.setUp()
        self.db = self.fixture.db
        self.app = self.fixture.app
        self.db.create_function('NOW', 0, lambda: '2026-10-19 10:00:00')
        for column in ["exam_start_time TEXT DEFAULT '2001-01-01'", "exam_end_time TEXT DEFAULT '2001-01-02'",
                       "created_at TEXT", "password_changed INT DEFAULT 1", "login_locked_until TEXT",
                       "password TEXT", "avatar_filename TEXT"]:
            self.db.execute('ALTER TABLE users ADD COLUMN '+column)
        self.db.commit()
        self.render = Mock(return_value='users')
        deps = dict(self.fixture.deps, render_template=self.render, build_initial_password=lambda username:'test-only',
                    generate_password_hash=lambda value:'test-hash', DEFAULT_AVATAR='default.png')
        spec=importlib.util.spec_from_file_location('student_schedules_under_test',Path(__file__).parents[1]/'routes/student.py')
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.app.register_blueprint(module.create_student_blueprint(deps))
        self.app.add_url_rule('/users-alias',endpoint='admin_user_management',view_func=lambda:'users')
        self.client=self.fixture.client

    def tearDown(self):
        self.fixture.tearDown()

    def test_user_list_uses_batch_schedule_instead_of_legacy_personal_times(self):
        self.assertEqual(self.client.get('/admin/users').status_code,200)
        users=self.render.call_args.kwargs['users']
        user=next(row for row in users if row['id']==1)
        self.assertNotIn('exam_start_time',user)
        self.assertEqual(user['exam_batches'][0]['batch_id'],7)
        self.assertEqual(user['exam_batches'][0]['start_time'].hour,9)
        self.assertEqual(next(row for row in users if row['id']==2)['exam_batches'],[])
        env=Environment(loader=ChoiceLoader([DictLoader({'base.html':'{% block content %}{% endblock %}'}),
                         FileSystemLoader(Path(__file__).parents[1]/'templates')]),undefined=StrictUndefined,autoescape=True)
        env.globals['url_for']=lambda endpoint,**kwargs:'/'+endpoint
        with self.app.test_request_context('/admin/users'):
            html=env.get_template('admin_user_management.html').render(**self.render.call_args.kwargs,request=request)
        self.assertIn('exam · default',html)
        self.assertNotIn('name="exam_start_time"',html)
        self.assertNotIn('name="update_exam_time"',html)

    def test_editing_user_preserves_legacy_columns_and_cannot_change_batch_time(self):
        response=self.client.post('/admin/users/1/edit',data=dict(username='1001',name='updated',
                                  exam_start_time='2030-01-01T12:00',exam_end_time='2030-01-02T12:00'))
        self.assertEqual(response.status_code,302)
        user=self.db.execute('SELECT name,exam_start_time,exam_end_time FROM users WHERE id=1').fetchone()
        self.assertEqual(tuple(user),('updated','2001-01-01','2001-01-02'))
        self.assertEqual(self.db.execute('SELECT start_time FROM exam_batches WHERE id=7').fetchone()[0],'2026-10-19 09:00')

    def test_obsolete_bulk_time_update_redirects_to_exam_arrangements_without_writes(self):
        response=self.client.post('/admin/users/batch_update',data=dict(filter_class_name='A',update_exam_time='1',teacher_name='NEW'))
        self.assertEqual(response.headers['Location'],'/admin/exams')
        self.assertEqual(self.db.execute('SELECT teacher_name FROM users WHERE id=1').fetchone()[0],'C')

    def test_course_bulk_update_still_works(self):
        response=self.client.post('/admin/users/batch_update',data=dict(filter_class_name='A',teacher_name='NEW'))
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.db.execute('SELECT teacher_name FROM users WHERE id=1').fetchone()[0],'NEW')
        self.assertEqual(self.db.execute('SELECT teacher_name FROM users WHERE id=3').fetchone()[0],'C')


if __name__=='__main__':
    unittest.main()
