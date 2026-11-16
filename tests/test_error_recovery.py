"""Regression coverage for legacy navigation and recovery without the database."""
from pathlib import Path
import importlib.util
import unittest
from unittest.mock import Mock

from flask import Flask, redirect, session, url_for
from services.error_recovery_service import install_error_recovery

ROOT = Path(__file__).parents[1]


class ErrorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=str(ROOT / 'templates'))
        self.app.secret_key = 'fixture'
        self.app.testing = False
        self.app.logger.disabled = True
        self.app.add_url_rule('/dashboard', 'dashboard', lambda: 'home')
        self.app.add_url_rule('/problem_ajax/<int:problem_id>', 'problem_ajax', lambda problem_id: 'question')
        self.app.add_url_rule('/seb/exit/<token>', 'exam.seb_shared_quit', lambda token: 'quit')
        self.app.add_url_rule('/seb/quit/<token>', 'exam.seb_quit', lambda token: 'quit')
        def fail():
            raise RuntimeError('private diagnostic')
        self.app.add_url_rule('/broken', view_func=fail)
        self.app.add_url_rule('/api/broken', 'api_broken', fail, methods=['POST'])
        self.exam = dict(id=9, seb_required=True, seb_config_mode='shared', seb_quit_token='independent')
        self.deps = dict(get_exam_by_id=Mock(return_value=self.exam),
                         is_seb_exam_required=lambda exam: bool(exam and exam.get('seb_required')),
                         get_shared_seb_settings=Mock(return_value=dict(seb_quit_token='shared')))
        install_error_recovery(self.app, self.deps)
        self.client = self.app.test_client()

    def login(self):
        with self.client.session_transaction() as state:
            state.update(user_id=25, selected_exam_id=9,
                         current_problem=dict(exam_id=9, display_number=2, token='original'))

    def test_error_preserves_answers_and_returns_to_current_question(self):
        self.login()
        response = self.client.get('/broken')
        self.assertEqual(response.status_code, 500)
        self.assertIn('/problem_ajax/2', response.text)
        self.assertIn('http://localhost/seb/exit/shared', response.text)
        self.assertNotIn('private diagnostic', response.text)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        with self.client.session_transaction() as state:
            self.assertEqual(state['current_problem']['token'], 'original')

    def test_independent_exam_uses_its_own_exit(self):
        self.login()
        self.exam['seb_config_mode'] = 'exam'
        self.assertIn('/seb/quit/independent', self.client.get('/broken').text)
        self.deps['get_shared_seb_settings'].assert_not_called()

    def test_cached_exit_survives_database_outage_and_broken_context_processor(self):
        self.login()
        with self.client.session_transaction() as state:
            state['seb_error_recovery'] = dict(exam_id=9, quit_url='http://localhost/seb/exit/saved')
        self.deps['get_exam_by_id'].side_effect = RuntimeError('database unavailable')
        @self.app.context_processor
        def broken_context():
            raise RuntimeError('context unavailable')
        response = self.client.get('/broken')
        self.assertEqual(response.status_code, 500)
        self.assertIn('/seb/exit/saved', response.text)
        self.deps['get_exam_by_id'].assert_not_called()

    def test_missing_database_still_shows_return_button(self):
        self.login()
        self.deps['get_exam_by_id'].side_effect = RuntimeError('database unavailable')
        response = self.client.get('/broken')
        self.assertEqual(response.status_code, 500)
        self.assertIn('/problem_ajax/2', response.text)
        self.assertNotIn('退出 SEB', response.text)

    def test_stale_exit_from_another_exam_is_ignored(self):
        self.login()
        with self.client.session_transaction() as state:
            state['seb_error_recovery'] = dict(exam_id=8, quit_url='http://localhost/seb/exit/stale')
        self.exam['seb_required'] = False
        response = self.client.get('/broken')
        self.assertNotIn('退出 SEB', response.text)
        self.assertNotIn('stale', response.text)

    def test_logged_out_error_returns_home_without_exit(self):
        response = self.client.get('/broken')
        self.assertIn('/dashboard', response.text)
        self.assertNotIn('退出 SEB', response.text)
        self.deps['get_exam_by_id'].assert_not_called()

    def test_api_post_failure_stays_json_and_does_not_read_exam(self):
        self.login()
        response = self.client.post('/api/broken')
        self.assertEqual(response.status_code, 500)
        self.assertIs(response.get_json()['success'], False)
        self.deps['get_exam_by_id'].assert_not_called()


class RecoveryEntryTests(unittest.TestCase):
    def test_real_question_route_caches_exit_for_database_failure(self):
        from test_seb_entry_routes import SebEntryRouteTests
        fixture = SebEntryRouteTests()
        fixture.setUp()
        fixture.app.testing = False
        fixture.app.logger.disabled = True
        fixture.app.template_folder = str(ROOT / 'templates')
        fixture.deps['get_exam_by_id'].return_value.update(seb_config_mode='shared')
        def fail():
            raise RuntimeError('database unavailable')
        fixture.app.add_url_rule('/broken', view_func=fail)
        install_error_recovery(fixture.app, fixture.deps)
        fixture.login_student()
        fixture.client.get('/seb/continue/9')
        self.assertEqual(fixture.client.get('/problem_ajax/1').status_code, 200)
        with fixture.client.session_transaction() as state:
            self.assertEqual(state['seb_error_recovery'], dict(
                exam_id=9, quit_url='http://localhost/seb/exit/shared-token'))
            token = state['current_problem']['token']
        fixture.deps['get_exam_by_id'].side_effect = RuntimeError('database unavailable')
        response = fixture.client.get('/broken')
        self.assertEqual(response.status_code, 500)
        self.assertIn('/seb/exit/shared-token', response.text)
        self.assertIn('/problem_ajax/2', response.text)
        with fixture.client.session_transaction() as state:
            self.assertEqual(state['current_problem']['token'], token)


class LegacyProfileTests(unittest.TestCase):
    def test_old_profile_redirects_without_querying_old_analytics(self):
        app = Flask(__name__)
        app.secret_key = 'fixture'
        spec = importlib.util.spec_from_file_location('student_recovery_test', ROOT / 'routes/student.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        query = Mock(side_effect=AssertionError('legacy SQL must not run'))
        app.register_blueprint(module.create_student_blueprint(dict(
            login_required=lambda view:view, redirect=redirect, url_for=url_for,
            get_student_profile=query)))
        app.add_url_rule('/student/answer-details', 'student_answer_details', lambda: 'personal data')
        with app.test_client() as client:
            response = client.get('/student/profile?user_id=999&exam_id=999')
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers['Location'], '/student/answer-details')
        query.assert_not_called()

    def test_navigation_only_keeps_my_data(self):
        template = (ROOT / 'templates/base.html').read_text(encoding='utf-8')
        self.assertNotIn("url_for('student_profile')", template)
        self.assertIn("url_for('student_answer_details')", template)


if __name__ == '__main__':
    unittest.main()
