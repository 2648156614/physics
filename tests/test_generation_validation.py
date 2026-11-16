import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from flask import Flask, Response, flash, redirect, request, session, url_for
from werkzeug.utils import secure_filename
from datetime import datetime

# Reuse the isolated dependency setup; tests never connect to production services.
from test_question_generation_service import question_generation_service as service
from services.generation_errors import TemplateGenerationError


def template(formula='6e-7*Q*q0/R', **changes):
    result = dict(id=27, template_name='fixture', problem_text='R={{R}}, Q={{Q}}, q0={{q0}}',
                  variables='R[12.5,12.5],Q[5,5],q0[5.5,5.5]', solution_formula=formula,
                  answer_count=1, answer_units='J', generation_strategy=None)
    result.update(changes)
    return result


class GenerationValidationTests(unittest.TestCase):
    def generate(self, item, attempts=3):
        with patch.object(service, 'get_template', return_value=item):
            return service.generate_problem_from_template(item['id'], attempts)

    def test_small_energy_keeps_precision_in_primary_and_fallback_paths(self):
        item = template()
        result = self.generate(item)
        self.assertEqual(result['correct_answers'], [1.32e-6])
        variables, ranges = service.parse_variable_specs(item['variables'])
        fallback = service.generate_fallback_problem(item, variables, service.build_formula_context(), ranges)
        self.assertEqual(fallback['correct_answers'], [1.32e-6])

    def test_missing_constants_are_reported_together_before_sampling(self):
        item = template('100*mp*v6*v_unit/(e*B)', variables='v6[1,8],B[0.1,0.4]')
        with patch.object(service.random, 'uniform') as sample:
            with self.assertRaisesRegex(TemplateGenerationError, 'e、mp、v_unit'):
                self.generate(item)
            sample.assert_not_called()

    def test_constant_zero_and_negative_work_are_legitimate(self):
        self.assertEqual(self.generate(template('0'))['correct_answers'], [0.0])
        self.assertEqual(self.generate(template('-6e-7*Q*q0/R'))['correct_answers'], [-1.32e-6])
        self.assertEqual(service.infer_answer_constraints(['J','N','W']), {})

    def test_finite_values_are_not_rejected_by_magnitude_or_parameter_ratio(self):
        for value in [1e-30, -1e-30, 1e25, -1e25, 0.0]:
            self.assertTrue(service.is_answer_reasonable_dynamic([value], {'x':20}, 0))
        for value in [float('nan'),float('inf'),float('-inf')]:
            self.assertFalse(service.is_answer_reasonable_dynamic([value]))

    def test_multiple_answers_preserve_precision_sign_and_zero(self):
        result = self.generate(template('1e-19,-2.345678901e-12,0', answer_count=3, answer_units='C,J,T'))
        self.assertEqual(result['correct_answers'], [1e-19,-2.345678901e-12,0.0])

    def test_mismatched_answer_count_never_repeats_first_answer(self):
        with self.assertRaisesRegex(TemplateGenerationError, '不一致'):
            self.generate(template('Q', answer_count=2))

    def test_formula_errors_and_nonfinite_results_never_return_default_zero(self):
        for formula in ['1/0', "float_unknown(Q)", "sp.oo"]:
            with self.subTest(formula=formula), self.assertRaises(TemplateGenerationError):
                self.generate(template(formula))

    def test_failed_fallback_midpoint_does_not_bypass_conditions(self):
        item = template('Q', generation_strategy=json.dumps({'answer_constraints':{'max_answer':1}}))
        variables, ranges = service.parse_variable_specs(item['variables'])
        with self.assertRaises(TemplateGenerationError):
            service.generate_fallback_problem(item, variables, service.build_formula_context(), ranges)

    def test_explicit_per_answer_bounds_use_signed_values_and_do_not_relax(self):
        rules = {'per_answer':[{'min_answer':1}, {'max_answer':-1}]}
        self.assertTrue(service.is_answer_reasonable_dynamic([2,-2], constraints=rules))
        self.assertFalse(service.is_answer_reasonable_dynamic([-2,2], attempt_num=999, constraints=rules))

    def test_template_variable_relationship_is_enforced(self):
        item = template('Q', generation_strategy=json.dumps({'variable_constraints':['Q < R']}))
        self.assertEqual(self.generate(item)['correct_answers'], [5.0])
        item['generation_strategy'] = json.dumps({'variable_constraints':['Q > R']})
        with self.assertRaises(TemplateGenerationError):
            self.generate(item)

    def test_small_variable_ranges_are_not_clamped_or_rounded_to_zero(self):
        item = template('q', variables='q[1e-19,1e-19]', answer_units='C')
        result = self.generate(item)
        self.assertEqual(result['var_values'], {'q':1e-19})
        self.assertEqual(result['correct_answers'], [1e-19])
        self.assertEqual(service.format_display_number(1e-19), 1e-19)

    def test_quoted_legacy_expressions_are_validated_and_still_work(self):
        self.assertEqual(self.generate(template("'Q/R'"))['correct_answers'], [.4])
        with self.assertRaisesRegex(TemplateGenerationError, 'mp'):
            self.generate(template("'mp*Q'"))

    def test_invalid_constraints_are_rejected_before_generation(self):
        for strategy in [{'answer_constraints':{'min_answer':5,'max_answer':2}},
                         {'answer_constraints':{'per_answer':[{},{}]}},
                         {'variable_constraints':['missing < R']},
                         {'variable_constraints':['sp.sin(Q) > 0']}]:
            with self.subTest(strategy=strategy), self.assertRaises(TemplateGenerationError):
                self.generate(template(generation_strategy=json.dumps(strategy)))

    def test_legacy_cache_is_recalculated_without_changing_parameters_or_source(self):
        item = template()
        cached = dict(template_id=27, var_values={'R':12.5,'Q':5,'q0':5.5},
                      correct_answers=[0.0], problem_text='original', answer_count=1)
        with patch.object(service, 'get_template', return_value=item):
            repaired = service.verify_cached_problem(cached)
        self.assertEqual(repaired['correct_answers'], [1.32e-6])
        self.assertEqual(repaired['var_values'], cached['var_values'])
        self.assertEqual(repaired['problem_text'], 'original')
        self.assertEqual(cached['correct_answers'], [0.0])
        with patch.object(service, 'get_template', side_effect=AssertionError('unnecessary lookup')):
            self.assertIs(service.verify_cached_problem(repaired), repaired)

    def test_invalid_legacy_cache_is_blocked(self):
        with patch.object(service, 'get_template', return_value=template('mu0*Q')):
            with self.assertRaisesRegex(TemplateGenerationError, 'mu0'):
                service.verify_cached_problem(dict(template_id=27, var_values={'Q':5}, correct_answers=[0]))

    def test_legacy_parameter_rounded_outside_declared_range_is_blocked(self):
        item = template('q',variables='q[1e-19,2e-19]')
        with patch.object(service,'get_template',return_value=item), self.assertRaises(TemplateGenerationError):
            service.verify_cached_problem(dict(template_id=27,var_values={'q':0},correct_answers=[0]))

    def test_redis_token_repair_keeps_same_token_and_persists_verified_answer(self):
        from test_redis_cache_service import redis_cache_service as cache
        original = dict(template_id=27, var_values={'R':12.5,'Q':5,'q0':5.5}, correct_answers=[0.0])
        stored = {'exam:problem:original-token':json.dumps(original)}
        redis = Mock()
        redis.get.side_effect = stored.get
        redis.setex.side_effect = lambda key, ttl, data: stored.__setitem__(key,data)
        with patch.object(cache, 'redis_client', redis), patch.object(service, 'get_template', return_value=template()):
            repaired = cache.get_problem_by_token('original-token')
        self.assertEqual(repaired['correct_answers'],[1.32e-6])
        self.assertEqual(json.loads(stored['exam:problem:original-token'])['var_values'],original['var_values'])
        self.assertEqual(len(stored),1)

    def test_invalid_redis_cache_is_not_stamped_or_used(self):
        from test_redis_cache_service import redis_cache_service as cache
        redis = Mock()
        redis.get.return_value = json.dumps(dict(template_id=27,var_values={'Q':5},correct_answers=[0]))
        with patch.object(cache, 'redis_client', redis), patch.object(service, 'get_template', return_value=template('mu0*Q')):
            with self.assertRaises(TemplateGenerationError):
                cache.get_problem_by_token('original-token')
        redis.setex.assert_not_called()

    def test_unverifiable_submission_does_not_save_or_count_an_attempt(self):
        from test_seb_entry_routes import SebEntryRouteTests
        fixture = SebEntryRouteTests()
        fixture.setUp()
        state = fixture.prepare_submission()
        state['get_problem_by_token'].side_effect = TemplateGenerationError('undefined mu0')
        response = fixture.client.post('/api/submit/2',json=dict(answer=0,time_taken=5))
        self.assertEqual(response.status_code,503)
        self.assertFalse(response.get_json()['saved'])
        self.assertIn('未通过验算',response.get_json()['message'])
        state['save_user_response'].assert_not_called()
        with fixture.client.session_transaction() as session_state:
            self.assertEqual(session_state['current_problem']['total_attempts'],0)
            self.assertEqual(session_state['current_problem']['token'],'original-token')


class AuthoringValidationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'fixture'
        self.conn = Mock()
        self.cursor = self.conn.cursor.return_value
        self.cursor.fetchone.return_value = dict(id=27,paper_id=1,template_name='original',problem_text='original')
        self.get_db = Mock(return_value=self.conn)
        spec = importlib.util.spec_from_file_location('question_validation_routes', Path(__file__).parents[1]/'routes/question.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.deps = dict(request=request,session=session,flash=flash,redirect=redirect,url_for=url_for,
                         login_required=lambda view=None,**kwargs: view if view else lambda f:f,
                         question_generation_service=service,get_db_connection=self.get_db,
                         infer_knowledge_label=lambda name:'fixture',get_exam_papers=lambda:[],
                         render_template=lambda *args,**kwargs:'form',json=json,datetime=datetime,
                         Response=Response,secure_filename=secure_filename,
                         save_imported_image=Mock(return_value=None),get_upload_folder_path=lambda:'.',
                         invalidate_exam_paper_cache=Mock(return_value=0),schedule_exam_paper_prewarm=Mock())
        self.app.register_blueprint(module.create_question_blueprint(self.deps))
        self.app.add_url_rule('/manage','admin_manage_problems',lambda:'manage')
        self.client = self.app.test_client()
        with self.client.session_transaction() as state:
            state.update(user_id=1,username='admin')

    def test_add_and_edit_reject_missing_constants_without_writing(self):
        for route in ['/admin/add_problem','/admin/edit_problem/27']:
            self.conn.reset_mock()
            response = self.client.post(route,data=dict(template_name='fixture',problem_text='test',
                variables='Q[1,2]',solution_formula='mu0*Q',answer_count='1',paper_id='1'))
            self.assertEqual(response.status_code,200)
            self.conn.commit.assert_not_called()
            mutations = [call for call in self.cursor.execute.call_args_list
                         if any(word in call.args[0].upper() for word in ['INSERT','UPDATE','DELETE'])]
            self.assertEqual(mutations,[])
            with self.client.session_transaction() as state:
                self.assertIn('mu0', str(state.get('_flashes')))

    def test_invalid_replacement_import_is_rejected_before_database_and_images(self):
        payload = dict(export_format='physics_exam_paper_backup',paper={'name':'fixture'},
                       templates=[template('mu0*Q')])
        response = self.client.post('/admin/exam_papers/import',data={
            'replace_existing':'1','paper_file':(io.BytesIO(json.dumps(payload).encode()),'paper.json')})
        self.assertEqual(response.status_code,302)
        self.get_db.assert_not_called()
        self.deps['save_imported_image'].assert_not_called()

    def test_valid_import_keeps_explicit_strategy(self):
        self.cursor.fetchone.return_value = None
        self.cursor.lastrowid = 28
        rules = {'answer_constraints':{'per_answer':[{'min_answer':0}]}, 'variable_constraints':['Q < R']}
        payload = dict(export_format='physics_exam_paper_backup',paper={'name':'fixture'},
                       templates=[template(generation_strategy=rules)])
        self.client.post('/admin/exam_papers/import',data={'paper_file':(
            io.BytesIO(json.dumps(payload).encode()),'paper.json')})
        self.conn.commit.assert_called_once()
        inserts = [call for call in self.cursor.execute.call_args_list if 'INSERT INTO problem_templates' in call.args[0]]
        self.assertEqual(json.loads(inserts[0].args[1][-1]),rules)


if __name__ == '__main__':
    unittest.main()
