import importlib.util
import logging
import os
from pathlib import Path
import time
from datetime import datetime
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
            'get_shared_seb_settings': Mock(return_value=dict(seb_config_key='b'*64, seb_quit_token='shared-token')),
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
            'RedisError': RuntimeError,
            'acquire_distributed_lock': Mock(return_value='lock'),
            'release_distributed_lock': Mock(),
        }
        self.receipts = {}
        self.deps['redis_client'] = Mock(
            get=Mock(side_effect=lambda key: self.receipts.get(key)),
            setex=Mock(side_effect=lambda key, ttl, value: self.receipts.__setitem__(key, value)),
        )
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

    def prepare_submission(self, save_success=True):
        self.login_student()
        # Blueprint dependencies are globals in the isolated route module.
        namespace = self.app.view_functions['exam.api_submit'].__wrapped__.__wrapped__.__globals__
        namespace.update({
            'get_problem_by_token': Mock(return_value={'template_id':200,'correct_answers':[4.0],'answer_count':1,'problem_text':'test'}),
            'is_problem_completed': Mock(return_value=False),
            'get_latest_attempt_count': Mock(return_value=0), 'is_correct': lambda answer, expected: answer == expected,
            'classify_error_type': Mock(return_value=None),
            'save_user_response': Mock(return_value=(save_success,[101])),
            'update_user_completion_status': Mock(return_value=False),
        })
        with self.client.session_transaction() as state:
            state['current_problem'] = dict(exam_id=9,paper_id=10,batch_id=7,user_id=25,display_number=2,
                                            actual_id=200,token='original-token',total_attempts=0)
        return namespace

    def test_successful_submission_receipt_replays_without_another_save(self):
        namespace=self.prepare_submission()
        payload=dict(answer=4,time_taken=5,submission_id='a'*32)
        response=self.client.post('/api/submit/2',json=payload)
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.get_json()['correct'])
        replay=self.client.post('/api/submit/2',json=payload)
        self.assertEqual(replay.get_json(),response.get_json())
        namespace['save_user_response'].assert_called_once()
        receipt=self.client.get('/api/submissions/'+'a'*32)
        self.assertEqual(receipt.get_json()['state'],'done')
        self.assertEqual(receipt.get_json()['data'],response.get_json())

    def test_save_failure_does_not_report_correct_or_advance_student(self):
        namespace=self.prepare_submission(save_success=False)
        response=self.client.post('/api/submit/2',json=dict(answer=4,time_taken=5,submission_id='b'*32))
        self.assertEqual(response.status_code,503)
        self.assertFalse(response.get_json()['saved'])
        self.assertNotIn('correct',response.get_json())
        namespace['update_user_completion_status'].assert_not_called()
        with self.client.session_transaction() as state:
            self.assertEqual(state['current_problem']['total_attempts'],0)
            self.assertEqual(state['current_problem']['token'],'original-token')

    def test_specific_seb_and_exam_time_failures_reach_api_client(self):
        self.prepare_submission()
        self.deps['validate_seb_exam_request'].return_value=(False,'SEB 配置需要更新')
        response=self.client.post('/api/submit/2',json=dict(answer=4,submission_id='c'*32))
        self.assertEqual(response.status_code,403)
        self.assertEqual(response.get_json()['message'],'SEB 配置需要更新')
        self.deps['get_user_exam_access'].return_value=dict(allowed=False,message='考试已结束',server_time=datetime.now(),exam_start_time=None,exam_status='ended')
        response=self.client.post('/api/submit/2',json=dict(answer=4,submission_id='d'*32))
        self.assertEqual(response.status_code,403)
        self.assertEqual(response.get_json()['message'],'考试已结束')

    def test_receipt_is_scoped_to_current_student_and_unknown_stays_unknown(self):
        self.prepare_submission()
        self.client.post('/api/submit/2',json=dict(answer=4,time_taken=5,submission_id='e'*32))
        with self.client.session_transaction() as state:
            state['user_id']=99
        self.assertEqual(self.client.get('/api/submissions/'+'e'*32).get_json()['state'],'unknown')

    def test_altered_answer_with_same_receipt_cannot_be_saved_twice(self):
        namespace=self.prepare_submission()
        self.client.post('/api/submit/2',json=dict(answer=4,time_taken=5,submission_id='f'*32))
        response=self.client.post('/api/submit/2',json=dict(answer=9,time_taken=5,submission_id='f'*32))
        self.assertEqual(response.status_code,409)
        namespace['save_user_response'].assert_called_once()

    def test_error_after_database_save_is_reported_as_saved(self):
        namespace=self.prepare_submission()
        namespace['update_user_completion_status'].side_effect=RuntimeError('test failure after save')
        response=self.client.post('/api/submit/2',json=dict(answer=4,time_taken=5,submission_id='9'*32))
        self.assertEqual(response.status_code,500)
        self.assertTrue(response.get_json()['saved'])
        self.assertIn('答案已保存',response.get_json()['message'])
        namespace['save_user_response'].assert_called_once()

    def test_receipt_service_outage_prevents_an_untracked_save(self):
        namespace=self.prepare_submission()
        self.deps['redis_client'].get.side_effect=RuntimeError('test redis unavailable')
        response=self.client.post('/api/submit/2',json=dict(answer=4,time_taken=5,submission_id='8'*32))
        self.assertEqual(response.status_code,503)
        namespace['save_user_response'].assert_not_called()

    def test_configuration_test_does_not_enter_exam_or_change_student_session(self):
        self.login_student()
        response = self.client.get('/seb/test/9')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.deps['render_template'].assert_called_once_with('seb_test.html', exam=self.deps['get_exam_by_id'].return_value)
        self.deps['get_user_exam_problem_display_info'].assert_not_called()
        self.deps['record_secure_exam_entry'].assert_not_called()
        self.deps['fetch_problem_from_pool'].assert_not_called()
        with self.client.session_transaction() as state:
            self.assertEqual(state['user_id'], 25)
            self.assertNotIn('pending_seb_exam_id', state)

    def test_configuration_test_still_requires_valid_seb_hash(self):
        self.deps['validate_seb_exam_request'].return_value = (False, 'invalid key')
        self.assertEqual(self.client.get('/seb/test/9').status_code, 403)
        self.deps['get_user_exam_problem_display_info'].assert_not_called()

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
