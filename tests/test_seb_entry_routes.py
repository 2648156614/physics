import importlib.util
import logging
import os
from pathlib import Path
import time
import unittest
from unittest.mock import Mock

from flask import Flask, flash, jsonify, redirect, request, session, url_for


class SebEntryRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'test-only'
        exam = {'id': 9, 'paper_id': 10, 'seb_required': True}
        access = {'allowed': True, 'paper_id': 10, 'batch_id': 7}
        mapping = {
            actual_id: {'actual_id': actual_id, 'display_number': number, 'paper_id': 10}
            for number, actual_id in enumerate([100, 200, 300], 1)
        }

        def login_required(view=None, *, db_check=False):
            if view is None:
                return login_required
            from functools import wraps

            @wraps(view)
            def wrapped(*args, **kwargs):
                if not session.get('user_id'):
                    return redirect(url_for('auth.login'))
                return view(*args, **kwargs)

            return wrapped

        self.deps = {
            'session': session, 'request': request, 'redirect': redirect,
            'url_for': url_for, 'flash': flash, 'jsonify': jsonify,
            'os': os, 'time': time, 'logger': logging.getLogger(__name__),
            'login_required': login_required,
            'get_exam_by_id': Mock(return_value=exam),
            'is_seb_exam_required': Mock(side_effect=lambda item: bool(item and item.get('seb_required'))),
            'validate_seb_exam_request': Mock(return_value=(True, None)),
            'get_user_available_exams': Mock(return_value=[exam]),
            'get_user_exam_access': Mock(return_value=access),
            'user_can_access_exam': Mock(return_value=True),
            'resolve_selected_exam_id': Mock(side_effect=lambda preferred=None, **kwargs: preferred or session.get('selected_exam_id')),
            'resolve_selected_exam_paper_id': Mock(return_value=10),
            'get_user_exam_problem_display_info': Mock(return_value=mapping),
            'get_completion_status_map': Mock(return_value={100: True, 200: False, 300: False}),
            'get_problem_by_token': Mock(return_value=None),
            'fetch_problem_from_pool': Mock(return_value=('test-token', {'template_id': 200, 'var_values': {}, 'answer_count': 1})),
            'generate_and_cache_problem': Mock(return_value=(None, None)),
            'is_problem_completed': Mock(return_value=False),
            'record_secure_exam_entry': Mock(return_value=True),
            'render_template': Mock(return_value='rendered question'),
        }
        spec = importlib.util.spec_from_file_location(
            'exam_routes_under_test', Path(__file__).parents[1] / 'routes/exam.py'
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.app.register_blueprint(module.create_exam_blueprint(self.deps))
        # Production exposes this legacy alias as well.
        self.app.add_url_rule('/problem_ajax/<int:problem_id>', endpoint='problem_ajax', view_func=self.app.view_functions['exam.problem_ajax'])
        self.app.add_url_rule('/dashboard', endpoint='dashboard', view_func=lambda: 'dashboard')
        self.app.add_url_rule('/login', endpoint='auth.login', view_func=lambda: 'login')
        self.client = self.app.test_client()

    def login_student(self):
        with self.client.session_transaction() as state:
            state['user_id'] = 25
            state['username'] = 'student'

    def test_start_always_requires_login_and_clears_previous_student(self):
        with self.client.session_transaction() as state:
            state.update(user_id=99, username='old-student', current_problem={'token': 'old'}, selected_exam_id=2, seb_entry_recorded_exam_id=9)
        response = self.client.get('/seb/start/9')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/login')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        with self.client.session_transaction() as state:
            self.assertEqual(dict(state), {'pending_seb_exam_id': 9})
        self.deps['get_user_exam_problem_display_info'].assert_not_called()
        self.deps['fetch_problem_from_pool'].assert_not_called()

    def test_invalid_seb_request_does_not_clear_existing_identity(self):
        self.login_student()
        self.deps['validate_seb_exam_request'].return_value = (False, 'invalid key')
        self.assertEqual(self.client.get('/seb/start/9').status_code, 403)
        with self.client.session_transaction() as state:
            self.assertEqual(state['user_id'], 25)
            self.assertNotIn('pending_seb_exam_id', state)

    def test_continue_defers_assignment_and_resumes_first_unfinished_question(self):
        self.login_student()
        response = self.client.get('/seb/continue/9')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/problem_ajax/1')
        self.deps['get_user_available_exams'].assert_called_once_with(25)
        self.deps['user_can_access_exam'].assert_not_called()
        self.deps['get_user_exam_problem_display_info'].assert_not_called()
        response = self.client.get(response.headers['Location'])
        self.assertEqual(response.status_code, 200)
        context = self.deps['render_template'].call_args.kwargs
        self.assertEqual(context['problem_id'], 2)
        self.assertEqual(context['resumed_problem_url'], '/problem_ajax/2')
        self.assertFalse(context['is_completed'])
        self.deps['get_user_exam_problem_display_info'].assert_called_once_with(25, 9, 10)
        self.deps['get_completion_status_map'].assert_called_once_with(25, 10, [100, 200, 300], 9)
        self.deps['fetch_problem_from_pool'].assert_called_once_with(200)
        self.deps['is_problem_completed'].assert_not_called()
        self.assertEqual(self.deps['get_user_available_exams'].call_count, 2)
        self.assertIn('problem;dur=', response.headers['Server-Timing'])
        with self.client.session_transaction() as state:
            self.assertNotIn('seb_resume_exam_id', state)
            self.assertEqual(state['current_problem']['user_id'], 25)
            self.assertEqual(state['current_problem']['display_number'], 2)

    def test_all_completed_resumes_last_question_for_finish_button(self):
        self.login_student()
        self.deps['get_completion_status_map'].return_value = {100: True, 200: True, 300: True}
        self.client.get('/seb/continue/9')
        response = self.client.get('/problem_ajax/1')
        self.assertEqual(response.status_code, 200)
        context = self.deps['render_template'].call_args.kwargs
        self.assertEqual(context['problem_id'], 3)
        self.assertTrue(context['is_completed'])

    def test_unassigned_or_not_started_students_do_not_receive_questions(self):
        self.login_student()
        self.deps['get_user_available_exams'].return_value = []
        response = self.client.get('/seb/continue/9')
        self.assertEqual(response.headers['Location'], '/dashboard')
        self.deps['fetch_problem_from_pool'].assert_not_called()
        self.deps['get_user_available_exams'].return_value = [{'id': 9, 'paper_id': 10}]
        self.deps['get_user_exam_access'].return_value = {'allowed': False, 'message': '考试尚未开始'}
        response = self.client.get('/seb/continue/9')
        self.assertIn('/dashboard', response.headers['Location'])
        self.deps['fetch_problem_from_pool'].assert_not_called()

    def test_resume_does_not_fall_back_to_another_exam(self):
        self.login_student()
        self.client.get('/seb/continue/9')
        self.deps['resolve_selected_exam_id'].return_value = 11
        self.deps['resolve_selected_exam_id'].side_effect = None
        response = self.client.get('/problem_ajax/1')
        self.assertEqual(response.headers['Location'], '/dashboard')
        self.deps['get_user_exam_problem_display_info'].assert_not_called()
        self.deps['fetch_problem_from_pool'].assert_not_called()

    def test_ordinary_question_navigation_does_not_auto_resume(self):
        self.login_student()
        with self.client.session_transaction() as state:
            state['selected_exam_id'] = 9
        response = self.client.get('/problem_ajax/3')
        self.assertEqual(response.status_code, 200)
        context = self.deps['render_template'].call_args.kwargs
        self.assertEqual(context['problem_id'], 3)
        self.assertIsNone(context['resumed_problem_url'])
        self.deps['get_completion_status_map'].assert_not_called()
        self.deps['get_user_available_exams'].assert_called_once_with(25)
        self.deps['is_problem_completed'].assert_called_once_with(25, 300, 10, 9)


if __name__ == '__main__':
    unittest.main()
