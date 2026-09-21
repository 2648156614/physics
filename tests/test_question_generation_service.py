import json
import math
import sys
import types
import unittest
from unittest.mock import patch

numpy_stub = types.ModuleType('numpy')
numpy_stub.isfinite = math.isfinite
sys.modules.setdefault('numpy', numpy_stub)

db_stub = types.ModuleType('db')
db_stub.get_db_connection = lambda: None
sys.modules.setdefault('db', db_stub)

from services.question_generation_service import (
    build_formula_context,
    format_display_number,
    generate_derived_inverse_problem,
    generate_inverse_problem,
    infer_generation_strategy,
)
from services import question_generation_service


def make_template(formula, answer_count, strategy):
    return {
        'id': 1,
        'template_name': 'test template',
        'problem_text': 'x={{x}}, y={{y}}',
        'solution_formula': formula,
        'answer_count': answer_count,
        'generation_strategy': json.dumps(strategy),
        'image_filename': None,
    }


def make_derived_template(strategy, problem_text=None):
    return {
        'id': 2,
        'template_name': 'derived inverse test',
        'problem_text': problem_text or 'R={{R}} m, s={{k}}t^2+1, T={{T}} s',
        'solution_formula': 'sqrt((2*k)**2 + (((2*k*T)**2)/R)**2)',
        'answer_count': 1,
        'generation_strategy': json.dumps(strategy),
        'image_filename': None,
    }


def make_derived_strategy():
    return {
        'enabled': True,
        'mode': 'derived_inverse_v1',
        'solve_for': 'q',
        'hidden_vars': {'q': {'range': [0.5, 10]}},
        'derived_vars': {
            'k': '3*q',
            'R': '(9/2)*q*T**2',
        },
        'target_answer': {'type': 'choice', 'values': [20]},
        'key_vars': {'T': {'type': 'choice', 'values': [2]}},
        'max_attempts': 5,
        'max_denominator': 16,
    }


def make_visible_solve_strategy(q_value, target_value):
    return {
        'enabled': True,
        'mode': 'derived_inverse_v1',
        'solve_for': 'a0',
        'hidden_vars': {
            'q': {
                'type': 'choice',
                'values': [q_value],
                'range': [1, 10],
            }
        },
        'derived_vars': {
            'h': '3*q',
            'L': '4*q',
        },
        'target_answer': {'type': 'choice', 'values': [target_value]},
        'key_vars': {},
        'beauty_constraints': {
            'display_style': 'decimal_first',
            'max_decimal_places': 3,
            'max_fraction_denominator': 4,
            'max_fraction_numerator': 20,
        },
        'max_attempts': 5,
    }


class InverseGenerationTests(unittest.TestCase):
    def test_legacy_single_answer_strategy_still_works(self):
        strategy = {
            'enabled': True,
            'mode': 'inverse_v1',
            'solve_for': 'x',
            'target_answer': {'type': 'choice', 'values': [8]},
        }
        result = generate_inverse_problem(
            make_template('x * 2', 1, strategy),
            ['x'],
            {'x': (1, 10)},
            build_formula_context(),
            ['m'],
            {},
        )

        self.assertIsNotNone(result)
        self.assertEqual(result['correct_answers'], [8.0])
        self.assertEqual(result['answer_count'], 1)

    def test_dependent_multi_answers_use_one_target(self):
        strategy = {
            'enabled': True,
            'mode': 'inverse_v1',
            'solve_for': 'x',
            'target_answers': [
                {'type': 'choice', 'values': [6]},
                None,
                None,
            ],
        }
        result = generate_inverse_problem(
            make_template('x, -x, 0', 3, strategy),
            ['x'],
            {'x': (1, 10)},
            build_formula_context(),
            ['V', 'V', 'V'],
            {},
        )

        self.assertIsNotNone(result)
        self.assertEqual(result['correct_answers'], [6.0, -6.0, 0.0])

    def test_independent_multi_answers_solve_multiple_variables(self):
        strategy = {
            'enabled': True,
            'mode': 'inverse_v1',
            'solve_for': ['x', 'y'],
            'target_answers': [
                {'type': 'choice', 'values': [10]},
                {'type': 'choice', 'values': [4]},
            ],
        }
        result = generate_inverse_problem(
            make_template('x + y, x - y', 2, strategy),
            ['x', 'y'],
            {'x': (1, 10), 'y': (1, 10)},
            build_formula_context(),
            ['', ''],
            {},
        )

        self.assertIsNotNone(result)
        self.assertEqual(result['correct_answers'], [10.0, 4.0])
        self.assertAlmostEqual(result['var_values']['x'], 7.0)
        self.assertAlmostEqual(result['var_values']['y'], 3.0)

    def test_inference_ignores_dependent_and_constant_answers(self):
        strategy = json.loads(
            infer_generation_strategy('test', '', 'x[1,10]', 'x, -x, 0', 3)
        )

        self.assertEqual(strategy['solve_for'], 'x')
        self.assertIsInstance(strategy['target_answers'][0], dict)
        self.assertIsNone(strategy['target_answers'][1])
        self.assertIsNone(strategy['target_answers'][2])

    def test_inference_targets_independent_answers(self):
        strategy = json.loads(
            infer_generation_strategy('test', '', 'x[1,10],y[1,10]', 'x + y, x - y', 2)
        )

        self.assertEqual(len(strategy['solve_for']), 2)
        self.assertTrue(all(isinstance(spec, dict) for spec in strategy['target_answers']))

    def test_inference_reduces_impossible_multi_target_plan(self):
        strategy = json.loads(
            infer_generation_strategy(
                'test',
                '',
                'v[1,50],AC[1,10],dBdt[0.1,10],B[0.1,5],x[1,10]',
                'B*v*(AC/100), (B*v*(AC/100)) + (dBdt*(x/100)*(AC/100)), 1',
                3,
            )
        )

        self.assertIsInstance(strategy['solve_for'], str)
        self.assertEqual(
            sum(isinstance(spec, dict) for spec in strategy['target_answers']),
            1,
        )


class DerivedInverseGenerationTests(unittest.TestCase):
    def generate(self, strategy=None, ranges=None, problem_text=None):
        return generate_derived_inverse_problem(
            make_derived_template(strategy or make_derived_strategy(), problem_text),
            ['k', 'R', 'T'],
            ranges or {'k': (1.5, 30), 'R': (0.5, 405), 'T': (0.5, 3)},
            build_formula_context(),
            ['m/s^2'],
            {'non_negative': True, 'min_answer': 0, 'max_answer': 1e7},
        )

    def test_hidden_variable_generates_visible_pretty_values(self):
        result = self.generate()

        self.assertIsNotNone(result)
        self.assertEqual(result['generation_mode'], 'derived_inverse_v1')
        self.assertEqual(result['var_values'], {'k': 6.0, 'R': 36.0, 'T': 2.0})
        self.assertEqual(result['display_var_values'], {'k': 6, 'R': 36, 'T': 2})
        self.assertAlmostEqual(result['correct_answers'][0], 20.0)
        self.assertNotIn('q', result['var_values'])
        self.assertNotIn('q', result['display_var_values'])
        self.assertNotIn('{{q}}', result['problem_text'])

    def test_derived_values_use_strict_visible_ranges(self):
        result = self.generate(
            ranges={'k': (1.5, 30), 'R': (0.5, 35), 'T': (0.5, 3)},
        )

        self.assertIsNone(result)

    def test_hidden_placeholder_in_problem_text_is_rejected(self):
        result = self.generate(problem_text='q={{q}}, R={{R}}, k={{k}}, T={{T}}')

        self.assertIsNone(result)

    def test_unsafe_derived_expression_is_rejected(self):
        strategy = make_derived_strategy()
        strategy['derived_vars']['k'] = "__import__('os').system('echo unsafe')"

        self.assertIsNone(self.generate(strategy=strategy))

    def test_main_generation_flow_dispatches_derived_mode(self):
        template = make_derived_template(make_derived_strategy())
        template['variables'] = 'k[1.5,30],R[0.5,405],T[0.5,3]'
        template['answer_units'] = 'm/s^2'

        with patch.object(question_generation_service, 'get_template', return_value=template):
            result = question_generation_service.generate_problem_from_template(template['id'])

        self.assertEqual(result['generation_mode'], 'derived_inverse_v1')
        self.assertNotIn('q', result['var_values'])

    def generate_visible_solve(self, q_value, target_value):
        template = {
            'id': 3,
            'template_name': 'visible inverse test',
            'problem_text': 'h={{h}} m, L={{L}} m, a0={{a0}} m/s^2',
            'solution_formula': 'a0 + (21/16)*(h/3 - 2)',
            'answer_count': 1,
            'generation_strategy': json.dumps(
                make_visible_solve_strategy(q_value, target_value)
            ),
            'image_filename': None,
        }
        return generate_derived_inverse_problem(
            template,
            ['h', 'L', 'a0'],
            {'h': (3, 30), 'L': (4, 40), 'a0': (1, 20)},
            build_formula_context(),
            ['m/s'],
            {'non_negative': True, 'min_answer': 0, 'max_answer': 1e7},
        )

    def test_sampled_hidden_variable_can_solve_visible_variable(self):
        result = self.generate_visible_solve(q_value=2, target_value=8)

        self.assertIsNotNone(result)
        self.assertEqual(result['var_values'], {'h': 6.0, 'L': 8.0, 'a0': 8.0})
        self.assertEqual(result['correct_answers'], [8.0])

    def test_visible_solve_keeps_finite_decimal(self):
        result = self.generate_visible_solve(q_value=8, target_value=14)

        self.assertIsNotNone(result)
        self.assertEqual(result['var_values'], {'h': 24.0, 'L': 32.0, 'a0': 6.125})
        self.assertEqual(result['display_var_values']['a0'], 6.125)
        self.assertEqual(result['correct_answers'], [14.0])
        self.assertNotIn('q', result['var_values'])
        self.assertNotIn('q', result['display_var_values'])
        self.assertNotIn('{{q}}', result['problem_text'])

    def test_decimal_display_is_preferred_over_fraction(self):
        self.assertEqual(format_display_number(5.88, max_denominator=25), 5.88)
        self.assertNotEqual(format_display_number(5.88, max_denominator=25), '147/25')

    def test_simple_recurring_fraction_is_allowed(self):
        self.assertEqual(format_display_number(1 / 3), '1/3')

    def test_failed_derived_mode_falls_back_to_legacy_generation(self):
        template = {
            'id': 4,
            'template_name': 'fallback test',
            'problem_text': 'x={{x}}',
            'variables': 'x[1,10]',
            'solution_formula': 'x * 2',
            'answer_count': 1,
            'answer_units': 'm',
            'generation_strategy': json.dumps({
                'enabled': True,
                'mode': 'derived_inverse_v1',
                'solve_for': 'x',
                'hidden_vars': {'q': {'range': [1, 10]}},
                'derived_vars': {'x': 'q'},
                'target_answer': {'type': 'choice', 'values': [8]},
            }),
            'image_filename': None,
        }

        with (
            patch.object(question_generation_service, 'get_template', return_value=template),
            patch.object(question_generation_service.random, 'uniform', return_value=2.0),
        ):
            result = question_generation_service.generate_problem_from_template(template['id'])

        self.assertIsNotNone(result)
        self.assertNotIn('generation_mode', result)
        self.assertEqual(result['var_values'], {'x': 2.0})
        self.assertEqual(result['correct_answers'], [4.0])


if __name__ == '__main__':
    unittest.main()
