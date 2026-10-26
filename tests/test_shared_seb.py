"""Shared SEB entry, authorization and persistent configuration regressions."""
import ast
from pathlib import Path
import unittest
import time
import sqlite3
from datetime import datetime
from unittest.mock import Mock

from flask import Flask, session, request, redirect, url_for, flash
import test_seb_entry_routes as entry
import test_exam_arrangement_routes as arrangement
from services.seb_exam_service import calculate_config_key_request_hash, validate_exam_config_request
from services.seb_settings_service import load_settings, save_settings, ensure_schema


class SharedEntryTests(unittest.TestCase):
    def setUp(self):
        entry.SebEntryRouteTests.setUp(self)

    def headers(self, path, key='b'*64):
        return {'X-SafeExamBrowser-ConfigKeyHash': calculate_config_key_request_hash('http://localhost'+path, key)}

    def login(self):
        entry.SebEntryRouteTests.login_student(self)

    def test_fixed_start_clears_previous_student_only_after_verification(self):
        self.login()
        self.assertEqual(self.client.get('/seb/start').status_code, 403)
        with self.client.session_transaction() as state:
            self.assertEqual(state['user_id'], 25)
        response = self.client.get('/seb/start', headers=self.headers('/seb/start'))
        self.assertEqual(response.headers['Location'], '/login')
        with self.client.session_transaction() as state:
            self.assertEqual(dict(state), {'pending_shared_seb': True})
        self.deps['fetch_problem_from_pool'].assert_not_called()

    def test_one_open_exam_redirects_without_generating_questions(self):
        self.login()
        self.deps['get_user_available_exams'].return_value = [dict(id=9, seb_required=True, seb_config_mode='shared', access_allowed=True)]
        response = self.client.get('/seb/exams', headers=self.headers('/seb/exams'))
        self.assertEqual(response.headers['Location'], '/seb/continue/9')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.deps['get_user_exam_problem_display_info'].assert_not_called()
        self.deps['record_secure_exam_entry'].assert_not_called()

    def test_multiple_open_exams_require_choice_and_exclude_independent_exams(self):
        self.login()
        shared = [dict(id=i, seb_required=True, seb_config_mode='shared', access_allowed=True) for i in (9, 10)]
        self.deps['get_user_available_exams'].return_value = shared + [dict(id=11, seb_required=True, access_allowed=True)]
        response = self.client.get('/seb/exams', headers=self.headers('/seb/exams'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.deps['render_template'].assert_called_once_with('seb_exams.html', exams=shared)

    def test_not_started_and_ended_exams_do_not_auto_enter(self):
        self.login()
        for status in ('not_started', 'ended'):
            self.deps['get_user_available_exams'].return_value = [dict(id=9, seb_required=True, seb_config_mode='shared', access_allowed=False, access_status=status)]
            self.assertEqual(self.client.get('/seb/exams', headers=self.headers('/seb/exams')).status_code, 200)
        self.deps['fetch_problem_from_pool'].assert_not_called()

    def test_no_assignment_shows_empty_list(self):
        self.login()
        self.deps['get_user_available_exams'].return_value = []
        self.assertEqual(self.client.get('/seb/exams', headers=self.headers('/seb/exams')).status_code, 200)
        self.deps['render_template'].assert_called_once_with('seb_exams.html', exams=[])

    def test_selection_requires_login_and_valid_hash_on_every_request(self):
        self.assertEqual(self.client.get('/seb/exams', headers=self.headers('/seb/exams')).status_code, 302)
        self.login()
        self.assertEqual(self.client.get('/seb/exams').status_code, 403)
        # A header signed for another URL is not sufficient.
        self.assertEqual(self.client.get('/seb/exams', headers=self.headers('/seb/start')).status_code, 403)

    def test_fixed_exit_token_survives_configuration_change(self):
        self.login()
        self.assertEqual(self.client.get('/seb/exit/wrong').status_code, 404)
        with self.client.session_transaction() as state:
            self.assertEqual(state['user_id'], 25)
        self.assertEqual(self.client.get('/seb/exit/shared-token').status_code, 200)
        with self.client.session_transaction() as state:
            self.assertFalse(state)

    def test_finish_uses_matching_shared_or_independent_quit_url(self):
        self.login()
        namespace = self.app.view_functions['exam.secure_finish'].__wrapped__.__globals__
        namespace['finish_secure_exam_session'] = Mock(return_value=(True, dict(completed_count=2, total_count=3)))
        exam = self.deps['get_exam_by_id'].return_value
        for mode, expected in [('shared', '/seb/exit/shared-token'), ('exam', '/seb/quit/legacy-token')]:
            exam.update(seb_config_mode=mode, seb_quit_token='legacy-token')
            with self.client.session_transaction() as state:
                state.update(selected_exam_id=9, current_problem=dict(exam_id=9, display_number=3))
            response = self.client.post('/api/exam/secure-finish')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['quit_url'], 'http://localhost'+expected)


class SharedLoginTests(unittest.TestCase):
    def test_actual_login_preserves_shared_launch_and_legacy_redirects(self):
        tree = ast.parse((Path(__file__).parents[1]/'routes/auth.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'login')
        node.decorator_list = []
        user = dict(id=25, username='student', password='fixture', password_changed=True)
        conn = Mock()
        conn.cursor.return_value.fetchone.return_value = user
        namespace = dict(session=session, request=request, redirect=redirect, url_for=url_for, flash=flash,
                         datetime=datetime, time=time, cleanup_expired_login_audits=Mock(),
                         get_db_connection=lambda: conn, is_password_hash=lambda p:False,
                         clear_exam_session_state=Mock(), UPGRADE_LEGACY_PASSWORD_ON_LOGIN=False,
                         enqueue_login_audit=Mock(), get_display_name=lambda u:u['username'],
                         DEFAULT_AVATAR='default.svg', is_strong_password=lambda p:True)
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'auth_login', 'exec'), namespace)
        app = Flask(__name__)
        app.secret_key = 'test-only'
        app.add_url_rule('/login', endpoint='auth.login', view_func=namespace['login'], methods=['POST'])
        app.add_url_rule('/seb/exams', endpoint='exam.seb_exams', view_func=lambda:'exams')
        app.add_url_rule('/seb/continue/<int:exam_id>', endpoint='exam.seb_continue', view_func=lambda exam_id:'exam')
        app.add_url_rule('/dashboard', endpoint='dashboard', view_func=lambda:'dashboard')
        client = app.test_client()
        for marker, expected in [(dict(pending_shared_seb=True), '/seb/exams'),
                                 (dict(pending_seb_exam_id=9), '/seb/continue/9'), ({}, '/dashboard')]:
            with client.session_transaction() as state:
                state.clear()
                state.update(marker)
            response = client.post('/login', data=dict(username='student', password='fixture'))
            self.assertEqual(response.headers['Location'], expected)


class SharedSettingsTests(unittest.TestCase):
    def setUp(self):
        arrangement.ArrangementRouteTests.setUp(self)

    def tearDown(self):
        arrangement.ArrangementRouteTests.tearDown(self)

    def test_schema_upgrade_keeps_existing_exams_and_shared_configuration(self):
        class SchemaCursor(arrangement.Cursor):
            def sql(self, sql):
                if sql.startswith('SHOW COLUMNS'):
                    return "SELECT name FROM pragma_table_info('exams') WHERE name='seb_config_mode'"
                return super().sql(sql)
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        try:
            db.execute('CREATE TABLE exams(id INT PRIMARY KEY, name TEXT)')
            db.execute("INSERT INTO exams VALUES(9,'existing')")
            cursor = SchemaCursor(db)
            ensure_schema(cursor)
            first = dict(db.execute('SELECT * FROM seb_settings').fetchone())
            db.execute("UPDATE seb_settings SET seb_config_key=? WHERE id=1", ('a'*64,))
            ensure_schema(cursor)
            second = dict(db.execute('SELECT * FROM seb_settings').fetchone())
            self.assertEqual(first['seb_quit_token'], second['seb_quit_token'])
            self.assertEqual(second['seb_config_key'], 'a'*64)
            self.assertEqual(db.execute('SELECT seb_config_mode FROM exams WHERE id=9').fetchone()[0], 'exam')
        finally:
            db.close()

    def test_settings_rotation_preserves_quit_token_and_explicit_revocation(self):
        connect = self.deps['get_db_connection']
        save_settings(connect, 'a'*64)
        save_settings(connect, 'b'*64)
        config = load_settings(connect)
        self.assertEqual(config['seb_quit_token'], 'shared-token')
        url = 'http://localhost/seb/start'
        self.assertTrue(validate_exam_config_request(config, url, calculate_config_key_request_hash(url, 'a'*64))[0])
        save_settings(connect, 'b'*64, True)
        self.assertFalse(validate_exam_config_request(load_settings(connect), url, calculate_config_key_request_hash(url, 'a'*64))[0])

    def test_invalid_setting_does_not_overwrite_previous_configuration(self):
        save_settings(self.deps['get_db_connection'], 'a'*64)
        with self.assertRaises(ValueError):
            save_settings(self.deps['get_db_connection'], 'invalid')
        self.assertEqual(load_settings(self.deps['get_db_connection'])['seb_config_key'], 'a'*64)

    def test_settings_endpoint_requires_admin(self):
        with self.client.session_transaction() as state:
            state['username'] = 'student'
        self.assertEqual(self.client.post('/admin/seb/settings', data={'config_key':'b'*64}).status_code, 403)
        self.assertIsNone(load_settings(self.deps['get_db_connection'])['seb_config_key'])

    def test_admin_setting_persists_and_shared_exam_can_be_created(self):
        self.assertEqual(self.client.post('/admin/seb/settings', data={'config_key':'b'*64}).status_code, 302)
        self.assertEqual(load_settings(self.deps['get_db_connection'])['seb_config_key'], 'b'*64)
        response = self.client.post('/admin/exams/create', data=dict(name='shared exam', paper_id='10', question_count='2',
                                  assign_type='class', assign_value='A', seb_required='on', seb_config_mode='shared'))
        self.assertEqual(response.status_code, 302)
        row = self.db.execute("SELECT seb_config_mode, seb_config_key FROM exams WHERE name='shared exam'").fetchone()
        self.assertEqual(row['seb_config_mode'], 'shared')
        self.assertIsNone(row['seb_config_key'])

    def test_existing_exam_keeps_independent_mode_until_explicitly_changed(self):
        arrangement.ArrangementRouteTests.post_exam(self, name='renamed')
        self.assertEqual(self.db.execute('SELECT seb_config_mode FROM exams WHERE id=9').fetchone()[0], 'exam')
        arrangement.ArrangementRouteTests.post_exam(self, seb_config_mode='shared')
        self.assertEqual(self.db.execute('SELECT seb_config_mode FROM exams WHERE id=9').fetchone()[0], 'shared')
        self.assertEqual(self.db.execute('SELECT answer FROM user_responses').fetchone()[0], 'saved-answer')


class SharedValidationTests(unittest.TestCase):
    def test_shared_key_cannot_unlock_independent_exam_or_bypass_request_hash(self):
        tree = ast.parse((Path(__file__).parents[1]/'app.py').read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'validate_seb_exam_request']
        namespace = dict(is_seb_exam_required=lambda e: bool(e and e.get('seb_required')),
                         get_shared_seb_settings=lambda: dict(seb_config_key='b'*64),
                         validate_exam_config_request=validate_exam_config_request)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'validation', 'exec'), namespace)
        validate = namespace['validate_seb_exam_request']
        url = 'http://localhost/api/submit/1'
        shared = dict(seb_required=True, seb_config_mode='shared', seb_config_key='a'*64)
        independent = dict(seb_required=True, seb_config_mode='exam', seb_config_key='a'*64)
        signed = calculate_config_key_request_hash(url, 'b'*64)
        self.assertTrue(validate(shared, url, signed)[0])
        self.assertFalse(validate(independent, url, signed)[0])
        self.assertFalse(validate(shared, url, None)[0])
        self.assertFalse(validate(shared, url, calculate_config_key_request_hash(url, 'a'*64))[0])


if __name__ == '__main__':
    unittest.main()
