import json
import math
import sys
import types
import unittest

numpy_stub = types.ModuleType('numpy')
numpy_stub.isfinite = math.isfinite
sys.modules.setdefault('numpy', numpy_stub)

db_stub = types.ModuleType('db')
db_stub.get_db_connection = lambda: None
sys.modules.setdefault('db', db_stub)

from services.question_generation_service import (
    build_formula_context,
    generate_inverse_problem,
    infer_generation_strategy,
)


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


if __name__ == '__main__':
    unittest.main()
