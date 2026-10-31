import copy
import json
import math
import os
import sys
import types
import unittest
from unittest.mock import patch

from ai_question_example import example
from services.ai_template_compiler import (CompileError, Expression, compile_isolated,
                                          compile_template, render_case, safe_html, unit_dimension)
from services.ai_compiled_runtime import generate_compiled, signature


class CompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.artifact = compile_isolated(**example())

    def test_circle_all_40_cross_combinations(self):
        result = self.artifact
        self.assertEqual(result['report']['candidate_count'], 40)
        self.assertEqual(result['report']['valid_count'], 40)
        self.assertEqual(result['report']['target_count'], 10)
        for case in result['template']['generation_strategy']['valid_cases']:
            v = case['var_values']
            answer = math.hypot(2*v['k'], (2*v['k']*v['T'])**2/v['R'])
            self.assertAlmostEqual(answer, case['correct_answers'][0])
            self.assertNotIn('q', v)

    def test_reject_arbitrary_python_and_expensive_powers(self):
        for formula in ["__import__('os').system('whoami')", 'x.__class__', '[x][0]',
                        '2**(2**8)', 'x**x', 'x**99', "'x'", '(lambda: 1)()']:
            with self.subTest(formula=formula), self.assertRaises(CompileError):
                Expression(formula, {'x'})

    def test_safe_expression_does_not_use_eval(self):
        with patch('builtins.eval', side_effect=AssertionError('eval forbidden')):
            self.assertEqual(Expression('sqrt(x**2)+sin(0)', {'x'}).value({'x': 3}), 3)

    def test_no_html_or_tex_script_injection(self):
        for text in ['<img src=x onerror=alert(1)>', '<script>alert(1)</script>',
                     '<p onclick="x">x</p>', r'<p>\href{javascript:x}{x}</p>']:
            with self.assertRaises(CompileError):
                safe_html(text)

    def test_wrong_units_are_rejected(self):
        payload = example()
        payload['analysis']['variable_units']['R'] = 's'
        with self.assertRaisesRegex(CompileError, '量纲'):
            compile_template(**payload)

    def test_missing_placeholder_or_hidden_leak_is_rejected(self):
        for text in ['{{R}} {{T}}', '{{R}} {{T}} {{k}} q']:
            payload = example(); payload['template']['text'] = text
            with self.assertRaises(CompileError):
                compile_template(**payload)

    def test_declared_bounds_are_not_expanded(self):
        payload = example(); payload['template']['variables'] = 'k[1.5,15],R[2.25,200],T[1,4]'
        result = compile_template(**payload)
        self.assertLess(result['report']['valid_count'], 40)
        self.assertTrue(all(case['var_values']['R'] <= 200 for case in result['template']['generation_strategy']['valid_cases']))

    def test_too_few_valid_cases_cannot_be_saved(self):
        payload = example(); payload['template']['generation_strategy']['key_vars']['T']['values'] = [1]
        with self.assertRaisesRegex(CompileError, '有效不同组合'):
            compile_template(**payload)

    def test_external_valid_cases_are_never_trusted(self):
        payload = example(); payload['template']['generation_strategy']['valid_cases'] = []
        with self.assertRaisesRegex(CompileError, '外部'):
            compile_template(**payload)

    def test_continuous_sampling_and_cycles_are_rejected(self):
        for change in ('continuous', 'cycle'):
            payload = example(); strategy = payload['template']['generation_strategy']
            if change == 'continuous':
                strategy['key_vars']['T'] = {'range': [1, 4]}
            else:
                strategy['derived_vars']['k'] = 'R/T**2'
            with self.assertRaises(CompileError):
                compile_template(**payload)

    def test_dimension_checker_handles_si_and_rejects_incompatible_sum(self):
        self.assertEqual(unit_dimension('m/s²'), unit_dimension('m*s**-2'))
        with self.assertRaises(CompileError):
            Expression('x+t', {'x','t'}).dimension({'x':unit_dimension('m'), 't':unit_dimension('s')})

    def test_independent_target_and_dependent_answers(self):
        payload = example(); template = payload['template']
        template['formula'] = '(' + template['formula'] + '),-(' + template['formula'] + '),0'
        template['answer_count'] = 3; template['answer_units'] = 'm/s²,m/s²,m/s²'
        result = compile_template(**payload)
        for case in result['template']['generation_strategy']['valid_cases']:
            a, b, c = case['correct_answers']; self.assertEqual(a, -b); self.assertEqual(c, 0)

    def test_joint_cases_keep_their_correlations(self):
        payload = example(); strategy = payload['template']['generation_strategy']
        strategy['key_vars'] = {}
        strategy['parameter_cases'] = [{'T':value} for value in [1,2,3,4]]
        result = compile_template(**payload)
        self.assertEqual(result['report']['valid_count'],40)

    def test_ordinary_inverse_uses_finite_cross_product(self):
        payload = {'template':{'name':'牛顿第二定律','text':'F={{F}} N, m={{m}} kg，求加速度。',
                   'variables':'F[1,40],m[1,4]','formula':'F/m','answer_count':1,'answer_units':'m/s²','image_filename':'',
                   'generation_strategy':{'enabled':True,'mode':'inverse_v1','solve_for':'F',
                       'target_answer':{'type':'choice','values':list(range(1,11))},
                       'key_vars':{'m':{'type':'choice','values':[1,2,3,4]}}}},
                   'analysis':{'variable_units':{'F':'N','m':'kg'},'constraints':[]}}
        result = compile_template(**payload)
        self.assertEqual(result['report']['valid_count'],40)

    def test_physical_constraints_remove_bad_cross_combinations(self):
        payload = example()
        payload['template']['generation_strategy']['hidden_vars']['t_limit'] = {'range':[3,3], 'type':'choice', 'values':[3]}
        payload['analysis']['variable_units']['t_limit'] = 's'
        payload['analysis']['constraints'] = [{'left':'T','op':'<=','right':'t_limit'}]
        result = compile_template(**payload)
        self.assertEqual(result['report']['valid_count'],30)
        self.assertTrue(all(case['var_values']['T'] <= 3 for case in result['template']['generation_strategy']['valid_cases']))
        payload['analysis']['constraints'] = [{'left':'R','op':'>','right':'0'}]
        self.assertEqual(compile_template(**payload)['report']['valid_count'],40)

    def test_compiled_runtime_uses_signed_cases_and_detects_changes(self):
        template = copy.deepcopy(self.artifact['template'])
        with patch.dict(os.environ, {'SECRET_KEY':'test-only'}):
            template['generation_strategy']['compiled_signature'] = signature(template)
            db = dict(id=12,template_name=template['name'],problem_text=template['text'],variables=template['variables'],
                      solution_formula=template['formula'],answer_count=template['answer_count'],
                      answer_units=template['answer_units'],image_filename=None)
            result = generate_compiled(db,template['generation_strategy'])
            self.assertEqual(result['template_id'],12)
            self.assertNotIn('q',result['var_units'])
            db['solution_formula'] = '__import__("os")'
            with self.assertRaisesRegex(CompileError,'签名'):
                generate_compiled(db,template['generation_strategy'])

    def test_student_path_never_reaches_old_generator(self):
        numpy_stub = types.ModuleType('numpy'); numpy_stub.isfinite = math.isfinite
        db_stub = types.ModuleType('db'); db_stub.get_db_connection = lambda: None
        sys.modules.setdefault('numpy', numpy_stub); sys.modules.setdefault('db', db_stub)
        from services import question_generation_service as generation
        template = copy.deepcopy(self.artifact['template'])
        with patch.dict(os.environ, {'SECRET_KEY':'test-only'}):
            template['generation_strategy']['compiled_signature'] = signature(template)
            db = dict(id=12,template_name=template['name'],problem_text=template['text'],variables=template['variables'],
                      solution_formula=template['formula'],answer_count=template['answer_count'],
                      answer_units=template['answer_units'],image_filename=None,generation_strategy=json.dumps(template['generation_strategy']))
            with patch.object(generation,'get_template',return_value=db), patch.object(generation,'build_formula_context',side_effect=AssertionError('legacy path')):
                self.assertEqual(generation.generate_problem_from_template(12)['answer_count'],1)
                db['generation_strategy'] = json.dumps({'strict':True})
                with self.assertRaises(CompileError):
                    generation.generate_problem_from_template(12)

    def test_compile_timeout_is_actionable(self):
        import subprocess
        with patch('subprocess.run',side_effect=subprocess.TimeoutExpired('compiler',1)):
            with self.assertRaisesRegex(CompileError,'时限'):
                compile_isolated(**example())


if __name__ == '__main__':
    unittest.main()
