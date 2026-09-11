import json
import random
import re
import time
from fractions import Fraction
from functools import lru_cache
from itertools import combinations
from math import log, pi
from urllib.parse import quote, unquote

import numpy as np
import sympy as sp

from db import get_db_connection


TEMPLATE_CACHE = {}
TEMPLATE_CACHE_TS = 0


def load_template_from_db(template_id):
    conn = get_db_connection()
    if not conn:
        return None
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM problem_templates WHERE id = %s", (template_id,))
    template = cursor.fetchone()
    cursor.close()
    conn.close()
    return template


def get_template(template_id):
    global TEMPLATE_CACHE, TEMPLATE_CACHE_TS
    if template_id in TEMPLATE_CACHE:
        return TEMPLATE_CACHE[template_id]

    template = load_template_from_db(template_id)
    if template:
        TEMPLATE_CACHE[template_id] = template
        TEMPLATE_CACHE_TS = time.time()
    return template


def clear_template_cache(template_id=None):
    global TEMPLATE_CACHE, TEMPLATE_CACHE_TS

    if template_id is None:
        TEMPLATE_CACHE = {}
    else:
        try:
            TEMPLATE_CACHE.pop(int(template_id), None)
        except (TypeError, ValueError):
            return TEMPLATE_CACHE_TS

    TEMPLATE_CACHE_TS = time.time()
    return TEMPLATE_CACHE_TS


def get_template_cache_ts():
    return TEMPLATE_CACHE_TS


def build_formula_context():
    x, t, h = sp.symbols('x t h')
    return {
        'x': x, 't': t, 'h': h, 'sp': sp,
        'sqrt': sp.sqrt, 'exp': sp.exp, 'integrate': sp.integrate, 'diff': sp.diff,
        'sin': sp.sin, 'cos': sp.cos, 'tan': sp.tan,
        'asin': sp.asin, 'acos': sp.acos, 'atan': sp.atan,
        'pi': pi, 'log': log, 'ln': sp.log,
        'abs': abs, 'Abs': sp.Abs, 'min': min, 'max': max, 'round': round,
        'pow': pow
    }


def evaluate_solution_formula(solution_formula, current_vars):
    raw_answer = eval(solution_formula, {}, current_vars)
    return coerce_formula_answers(raw_answer, current_vars)


def coerce_formula_answers(raw_answer, current_vars, depth=0):
    if depth > 3:
        raise ValueError('solution formula has too many nested string expressions')

    if isinstance(raw_answer, str):
        stripped_answer = raw_answer.strip()
        if not stripped_answer:
            raise ValueError('solution formula returned an empty string')
        try:
            return [float(stripped_answer)]
        except ValueError:
            evaluated_answer = eval(stripped_answer, {}, current_vars)
            return coerce_formula_answers(evaluated_answer, current_vars, depth + 1)

    if isinstance(raw_answer, (tuple, list)):
        normalized_answers = []
        for answer_item in raw_answer:
            normalized_answers.extend(coerce_formula_answers(answer_item, current_vars, depth + 1))
        return normalized_answers

    if hasattr(raw_answer, 'evalf'):
        raw_answer = raw_answer.evalf()

    return [float(raw_answer)]


def parse_generation_strategy(template):
    raw_strategy = (template or {}).get('generation_strategy')
    if not raw_strategy:
        return None
    if isinstance(raw_strategy, dict):
        return raw_strategy
    try:
        strategy = json.loads(raw_strategy)
    except (TypeError, ValueError):
        return None
    return strategy if isinstance(strategy, dict) else None


def format_display_number(value, max_denominator=12):
    if not isinstance(value, (int, float)) or not np.isfinite(value):
        return value
    if abs(value - round(value)) < 1e-9:
        return int(round(value))
    fraction = Fraction(float(value)).limit_denominator(max_denominator)
    if abs(float(fraction) - value) < 1e-8 and abs(fraction.denominator) > 1:
        return f"{fraction.numerator}/{fraction.denominator}"
    if abs(value) >= 1:
        return round(value, 3)
    return round(value, 4)


def _rand_from_spec(spec, fallback_range, rng=None):
    rng = rng or random
    spec = spec or {}
    min_val, max_val = spec.get('range', fallback_range)
    step = spec.get('step', 1)
    value_type = spec.get('type', 'integer')
    if value_type == 'choice' and spec.get('values'):
        return rng.choice(spec['values'])
    if value_type == 'fraction':
        denominator = int(spec.get('denominator', 2) or 2)
        low = int(round(float(min_val) * denominator))
        high = int(round(float(max_val) * denominator))
        return rng.randint(low, high) / denominator
    if value_type == 'decimal':
        precision = int(spec.get('precision', 2) or 2)
        return round(rng.uniform(float(min_val), float(max_val)), precision)

    step = int(step or 1)
    low = int(round(float(min_val)))
    high = int(round(float(max_val)))
    if high < low:
        low, high = high, low
    choices = list(range(low, high + 1, max(1, step)))
    return rng.choice(choices or [low])


def _get_symbolic_formula_parts(solution_formula, variables):
    symbols = {name: sp.symbols(name) for name in variables}
    context = build_formula_context()
    context.update(symbols)
    raw_expressions = eval(solution_formula, {}, context)
    expressions = list(raw_expressions) if isinstance(raw_expressions, (tuple, list)) else [raw_expressions]
    return [sp.sympify(expr) for expr in expressions], symbols


@lru_cache(maxsize=256)
def _prepare_inverse_solver(solution_formula, variables, solve_for, target_indices):
    expressions, symbols = _get_symbolic_formula_parts(solution_formula, variables)
    solve_symbols = [symbols[name] for name in solve_for]
    target_symbols = sp.symbols(f'_inverse_target_0:{len(target_indices)}')
    equations = [
        sp.Eq(expressions[answer_index], target_symbol)
        for answer_index, target_symbol in zip(target_indices, target_symbols)
    ]
    solutions = sp.solve(equations, solve_symbols, dict=True)
    if isinstance(solutions, dict):
        solutions = [solutions]
    return symbols, solve_symbols, target_symbols, tuple(solutions or [])


def _solve_formula_for_vars(solution_formula, variables, solve_for, assigned_vars, target_answers):
    target_indices = tuple(sorted(target_answers))
    if not target_indices or len(target_indices) != len(solve_for):
        return None

    try:
        symbols, solve_symbols, target_symbols, solutions = _prepare_inverse_solver(
            solution_formula,
            tuple(variables),
            tuple(solve_for),
            target_indices,
        )
    except Exception:
        return None
    if not solutions:
        return None

    assigned_substitutions = {
        symbols[name]: value
        for name, value in assigned_vars.items()
        if name in symbols
    }
    assigned_substitutions.update({
        target_symbol: target_answers[answer_index]
        for answer_index, target_symbol in zip(target_indices, target_symbols)
    })
    for solution in solutions:
        solved_values = {}
        substitutions = dict(assigned_substitutions)
        substitutions.update(solution)
        valid_solution = True
        for name, symbol in zip(solve_for, solve_symbols):
            solved_expr = solution.get(symbol)
            if solved_expr is None:
                valid_solution = False
                break
            try:
                evaluated = sp.N(solved_expr.subs(substitutions))
                if evaluated.is_real is False:
                    valid_solution = False
                    break
                value = float(evaluated)
            except Exception:
                valid_solution = False
                break
            if not np.isfinite(value):
                valid_solution = False
                break
            solved_values[name] = value

        if valid_solution and len(solved_values) == len(solve_for):
            return solved_values
    return None


def _normalize_solve_for(strategy, variables):
    raw_solve_for = strategy.get('solve_for')
    if isinstance(raw_solve_for, str):
        solve_for = [raw_solve_for]
    elif isinstance(raw_solve_for, (tuple, list)):
        solve_for = [name for name in raw_solve_for if isinstance(name, str)]
    else:
        return []

    unique_solve_for = []
    for name in solve_for:
        if name in variables and name not in unique_solve_for:
            unique_solve_for.append(name)
    return unique_solve_for


def _normalize_target_specs(strategy, answer_count):
    target_specs = {}
    raw_target_answers = strategy.get('target_answers')
    if isinstance(raw_target_answers, (tuple, list)):
        for answer_index, spec in enumerate(raw_target_answers[:answer_count]):
            if isinstance(spec, dict):
                target_specs[answer_index] = spec
    elif isinstance(raw_target_answers, dict):
        for raw_index, spec in raw_target_answers.items():
            if not isinstance(spec, dict):
                continue
            try:
                parsed_index = int(raw_index)
            except (TypeError, ValueError):
                continue
            answer_index = parsed_index - 1 if parsed_index >= 1 else parsed_index
            if 0 <= answer_index < answer_count:
                target_specs[answer_index] = spec

    if not target_specs and isinstance(strategy.get('target_answer'), dict):
        target_specs[0] = strategy['target_answer']
    return target_specs


def _solved_value_is_reasonable(name, value, configured_ranges):
    min_val, max_val = configured_ranges.get(name, get_adaptive_default_range(name))
    relaxed_min = min_val * 0.2
    relaxed_max = max_val * 5
    return value > 0 and relaxed_min <= value <= relaxed_max


def _find_solve_vars(expressions, symbols, variables, preferred_solve_order, target_indices):
    ordered_variables = []
    for name in preferred_solve_order + variables:
        if name in variables and name not in ordered_variables:
            ordered_variables.append(name)

    target_expressions = [expressions[index] for index in target_indices]
    for solve_for in combinations(ordered_variables, len(target_indices)):
        solve_symbols = [symbols[name] for name in solve_for]
        try:
            determinant = sp.simplify(
                sp.Matrix(target_expressions).jacobian(solve_symbols).det()
            )
        except Exception:
            continue
        if determinant != 0:
            return list(solve_for)
    return []


def _select_inverse_targets_and_vars(expressions, symbols, variables, preferred_solve_order):
    variable_symbols = [symbols[name] for name in variables]
    target_indices = []
    current_rank = 0

    for answer_index, expression in enumerate(expressions):
        if not expression.free_symbols.intersection(variable_symbols):
            continue
        candidate_expressions = [expressions[index] for index in target_indices] + [expression]
        try:
            candidate_rank = sp.Matrix(candidate_expressions).jacobian(variable_symbols).rank()
        except Exception:
            continue
        if candidate_rank > current_rank:
            target_indices.append(answer_index)
            current_rank = candidate_rank

    if not target_indices:
        return [], []

    solve_for = _find_solve_vars(
        expressions,
        symbols,
        variables,
        preferred_solve_order,
        target_indices,
    )
    return (target_indices, solve_for) if solve_for else ([], [])


def _build_inferred_key_vars(variables, solve_for, configured_ranges):
    key_vars = {}
    for var in variables:
        if var in solve_for:
            continue
        min_val, max_val = configured_ranges.get(var, get_adaptive_default_range(var))
        low = max(1, int(round(min_val)))
        high = max(low, int(round(min(max_val, 50))))
        key_vars[var] = {'type': 'integer', 'range': [low, high], 'step': 1}
    return key_vars


def _inverse_plan_is_feasible(
    solution_formula,
    variables,
    configured_ranges,
    solve_for,
    target_indices,
    key_vars,
    target_spec,
    attempts=24,
):
    rng = random.Random(0)
    for _ in range(attempts):
        targets = {
            answer_index: _rand_from_spec(target_spec, (1, 80), rng)
            for answer_index in target_indices
        }
        assigned_vars = {}
        for var in variables:
            if var in solve_for:
                continue
            fallback_range = configured_ranges.get(var, get_adaptive_default_range(var))
            assigned_vars[var] = _rand_from_spec(key_vars.get(var), fallback_range, rng)
        solved_values = _solve_formula_for_vars(
            solution_formula,
            variables,
            solve_for,
            assigned_vars,
            targets,
        )
        if solved_values and all(
            _solved_value_is_reasonable(name, value, configured_ranges)
            for name, value in solved_values.items()
        ):
            return True
    return False


def generate_inverse_problem(template, variables, configured_ranges, local_vars, answer_units, answer_constraints):
    strategy = parse_generation_strategy(template)
    if not strategy or not strategy.get('enabled', True) or strategy.get('mode') != 'inverse_v1':
        return None

    answer_count = int(template.get('answer_count', 1) or 1)
    solve_for = _normalize_solve_for(strategy, variables)
    target_specs = _normalize_target_specs(strategy, answer_count)
    if not solve_for or len(solve_for) != len(target_specs):
        return None

    key_vars = strategy.get('key_vars') or {}
    free_vars = strategy.get('free_vars') or {}
    max_attempts = int(strategy.get('max_attempts', 30) or 30)
    var_units = infer_variable_units(template.get('problem_text', ''), variables)
    if len(answer_units) < answer_count:
        answer_units.extend([''] * (answer_count - len(answer_units)))
    elif len(answer_units) > answer_count:
        answer_units = answer_units[:answer_count]

    for attempt in range(max_attempts):
        target_answers = {
            answer_index: _rand_from_spec(spec, spec.get('range', (1, 50)))
            for answer_index, spec in target_specs.items()
        }
        var_values = {}
        for var in variables:
            if var in solve_for:
                continue
            spec = key_vars.get(var) or free_vars.get(var) or {}
            fallback_range = configured_ranges.get(var, get_adaptive_default_range(var))
            var_values[var] = _rand_from_spec(spec, fallback_range)

        solved_values = _solve_formula_for_vars(
            template['solution_formula'],
            variables,
            solve_for,
            var_values,
            target_answers,
        )
        if not solved_values:
            continue
        if any(
            not _solved_value_is_reasonable(name, value, configured_ranges)
            for name, value in solved_values.items()
        ):
            continue

        var_values.update({name: round(value, 8) for name, value in solved_values.items()})
        current_vars = local_vars.copy()
        current_vars.update(var_values)
        try:
            correct_answers = evaluate_solution_formula(template['solution_formula'], current_vars)
        except Exception:
            continue
        if len(correct_answers) != answer_count:
            continue
        if not is_answer_reasonable_dynamic(correct_answers, var_values, attempt, answer_constraints):
            continue
        targets_match = all(
            abs(correct_answers[answer_index] - float(target_answer))
            <= max(1e-6, abs(float(target_answer)) * 1e-6)
            for answer_index, target_answer in target_answers.items()
        )
        if not targets_match:
            continue

        display_values = {
            key: format_display_number(value, strategy.get('max_denominator', 12))
            for key, value in var_values.items()
        }
        problem_content = format_problem_text(template['problem_text'], display_values)
        return {
            'problem_text': problem_content,
            'var_values': var_values,
            'display_var_values': display_values,
            'var_units': var_units,
            'correct_answers': correct_answers,
            'answer_units': answer_units,
            'template_id': template['id'],
            'answer_count': answer_count,
            'template_name': template['template_name'],
            'image_filename': template.get('image_filename'),
            'generation_mode': 'inverse_v1'
        }

    return None


def infer_generation_strategy(template_name, problem_text, variables_text, solution_formula, answer_count=1):
    variables, configured_ranges = parse_variable_specs(variables_text or '')
    answer_count = int(answer_count or 1)
    if answer_count < 1 or not variables:
        return ''

    preferred_solve_order = ['i', 'I', 'R', 'B', 'v', 'L', 'l', 'r', 'a', 'x', 'AC', 'omega']
    try:
        expressions, symbols = _get_symbolic_formula_parts(solution_formula, variables)
    except Exception:
        return ''
    if len(expressions) != answer_count:
        return ''

    independent_target_indices, _ = _select_inverse_targets_and_vars(
        expressions,
        symbols,
        variables,
        preferred_solve_order,
    )
    if not independent_target_indices:
        return ''

    target_spec = {'type': 'integer', 'range': [1, 80], 'step': 1}
    target_answer_indices = []
    solve_for = []
    key_vars = {}
    for target_count in range(len(independent_target_indices), 0, -1):
        candidate_targets = independent_target_indices[:target_count]
        candidate_solve_for = _find_solve_vars(
            expressions,
            symbols,
            variables,
            preferred_solve_order,
            candidate_targets,
        )
        if not candidate_solve_for:
            continue
        candidate_key_vars = _build_inferred_key_vars(
            variables,
            candidate_solve_for,
            configured_ranges,
        )
        if _inverse_plan_is_feasible(
            solution_formula,
            variables,
            configured_ranges,
            candidate_solve_for,
            candidate_targets,
            candidate_key_vars,
            target_spec,
        ):
            target_answer_indices = candidate_targets
            solve_for = candidate_solve_for
            key_vars = candidate_key_vars
            break
    if not target_answer_indices or not solve_for:
        return ''

    free_vars = {}
    strategy = {
        'enabled': True,
        'mode': 'inverse_v1',
        'solve_for': solve_for[0] if len(solve_for) == 1 else solve_for,
        'key_vars': key_vars,
        'free_vars': free_vars,
        'max_attempts': 30,
        'max_denominator': 12
    }
    if answer_count == 1:
        strategy['target_answer'] = target_spec
    else:
        strategy['target_answers'] = [
            target_spec if answer_index in target_answer_indices else None
            for answer_index in range(answer_count)
        ]
    return json.dumps(strategy, ensure_ascii=False, indent=2)


def generate_problem_from_template(template_id, max_attempts=10):
    template = get_template(template_id)

    if not template:
        return None

    variables, configured_ranges = parse_variable_specs(template.get('variables', ''))
    local_vars = build_formula_context()

    reasonable_ranges = {}
    for var in variables:
        reasonable_ranges[var] = configured_ranges.get(var, get_adaptive_default_range(var))

    answer_units = parse_answer_units(template)
    answer_constraints = infer_answer_constraints(answer_units)
    var_units = infer_variable_units(template.get('problem_text', ''), variables)

    inverse_problem = generate_inverse_problem(
        template,
        variables,
        configured_ranges,
        local_vars,
        answer_units[:],
        answer_constraints,
    )
    if inverse_problem:
        print(f"Inverse problem generated - template: {template['template_name']}")
        print(f"   Variable values: {inverse_problem['var_values']}")
        print(f"   Correct answers: {inverse_problem['correct_answers']}")
        return inverse_problem

    for attempt in range(max_attempts):
        var_values = {}

        for var in variables:
            min_val, max_val = reasonable_ranges[var]
            range_expansion = 1.0 + (attempt * 0.1)
            expanded_min = max(0.01, min_val / range_expansion)
            expanded_max = max_val * range_expansion

            var_values[var] = round(random.uniform(expanded_min, expanded_max), 2)

        problem_content = format_problem_text(template['problem_text'], var_values)

        try:
            current_vars = local_vars.copy()
            current_vars.update(var_values)

            correct_answers = evaluate_solution_formula(template['solution_formula'], current_vars)

            answer_count = template.get('answer_count', 1)
            if len(correct_answers) != answer_count:
                correct_answers = [correct_answers[0]] * answer_count

            if is_answer_reasonable_dynamic(correct_answers, var_values, attempt, answer_constraints):
                for var, value in var_values.items():
                    current_min, current_max = reasonable_ranges[var]
                    reasonable_ranges[var] = (
                        min(current_min, value * 0.8),
                        max(current_max, value * 1.2)
                    )

                answer_count = template.get('answer_count', 1)
                if len(answer_units) < answer_count:
                    answer_units.extend([''] * (answer_count - len(answer_units)))
                elif len(answer_units) > answer_count:
                    answer_units = answer_units[:answer_count]

                formatted_correct_answers = []
                for answer in correct_answers:
                    abs_answer = abs(answer)
                    if abs_answer == 0:
                        formatted_correct_answers.append(0.0)
                    elif abs_answer >= 1000:
                        formatted_correct_answers.append(round(answer, 0))
                    elif abs_answer >= 1:
                        formatted_correct_answers.append(round(answer, 2))
                    elif abs_answer >= 0.01:
                        formatted_correct_answers.append(round(answer, 4))
                    else:
                        formatted_correct_answers.append(round(answer, 6))

                result_data = {
                    'problem_text': problem_content,
                    'var_values': var_values,
                    'var_units': var_units,
                    'correct_answers': formatted_correct_answers,
                    'answer_units': answer_units,
                    'template_id': template_id,
                    'answer_count': template.get('answer_count', 1),
                    'template_name': template['template_name'],
                    'image_filename': template.get('image_filename')
                }

                print(f"Problem generated successfully - template: {template['template_name']}")
                print(f"   Variable values: {var_values}")
                print(f"   Correct answers: {formatted_correct_answers}")
                print(f"   Answer units: {answer_units}")
                print(f"   Answer count: {answer_count}")

                return result_data

        except Exception as e:
            print(f"Problem generation attempt {attempt + 1} failed: {str(e)}")
            continue

    return generate_fallback_problem(template, variables, local_vars, reasonable_ranges, answer_units, answer_constraints)


def generate_fallback_problem(template, variables, local_vars, reasonable_ranges=None, answer_units=None, answer_constraints=None):
    answer_units = answer_units or parse_answer_units(template)
    answer_constraints = answer_constraints or infer_answer_constraints(answer_units)
    var_units = infer_variable_units(template.get('problem_text', ''), variables)

    correct_answers = [0.0] * template.get('answer_count', 1)
    problem_content = template['problem_text']

    for attempt in range(5):
        var_values = {}
        for var in variables:
            min_val, max_val = (reasonable_ranges or {}).get(var, (1.0, 3.0))
            var_values[var] = round(random.uniform(min_val, max_val), 2)

        problem_content = format_problem_text(template['problem_text'], var_values)

        try:
            current_vars = local_vars.copy()
            current_vars.update(var_values)
            current_answers = evaluate_solution_formula(template['solution_formula'], current_vars)

            answer_count = template.get('answer_count', 1)
            if len(current_answers) != answer_count:
                current_answers = [current_answers[0]] * answer_count

            if is_answer_reasonable_dynamic(current_answers, var_values, attempt, answer_constraints):
                correct_answers = current_answers
                break
        except Exception:
            continue
    else:
        var_values = {}
        for var in variables:
            min_val, max_val = (reasonable_ranges or {}).get(var, (1.0, 3.0))
            var_values[var] = round((min_val + max_val) / 2, 2)
        problem_content = format_problem_text(template['problem_text'], var_values)

        try:
            current_vars = local_vars.copy()
            current_vars.update(var_values)
            fallback_answers = evaluate_solution_formula(template['solution_formula'], current_vars)
            answer_count = template.get('answer_count', 1)
            if len(fallback_answers) != answer_count:
                fallback_answers = [fallback_answers[0]] * answer_count
            correct_answers = fallback_answers
        except Exception as e:
            print(f"Fallback problem formula calculation still failed, using default answers: {str(e)}")

    answer_count = template.get('answer_count', 1)
    if len(answer_units) < answer_count:
        answer_units.extend([''] * (answer_count - len(answer_units)))
    elif len(answer_units) > answer_count:
        answer_units = answer_units[:answer_count]

    formatted_correct_answers = []
    for answer in correct_answers:
        abs_answer = abs(answer)
        if abs_answer >= 1000:
            formatted_correct_answers.append(round(answer, 0))
        elif abs_answer >= 1:
            formatted_correct_answers.append(round(answer, 2))
        else:
            formatted_correct_answers.append(round(answer, 4))

    result_data = {
        'problem_text': problem_content,
        'var_values': var_values,
        'var_units': var_units,
        'correct_answers': formatted_correct_answers,
        'answer_units': answer_units,
        'template_id': template['id'],
        'answer_count': template.get('answer_count', 1),
        'template_name': template['template_name'],
        'image_filename': template.get('image_filename')
    }

    print(f"Fallback problem generated - template: {template['template_name']}")
    print(f"   Variable values: {var_values}")
    print(f"   Correct answers: {formatted_correct_answers}")
    print(f"   Answer units: {answer_units}")

    return result_data


def get_adaptive_default_range(var_name):
    var_lower = var_name.lower()

    if any(char in var_lower for char in ['r', 'a', 'l', 'd', 'x', 'h']):
        return (0.1, 10.0)
    elif any(char in var_lower for char in ['v', 'u', 'w', 'speed', 'velocity']):
        return (1.0, 50.0)
    elif any(char in var_lower for char in ['b', 'e', 'f', 'field']):
        return (0.1, 5.0)
    elif any(char in var_lower for char in ['i', 'current']):
        return (0.1, 10.0)
    elif any(char in var_lower for char in ['r', 'resistance']):
        return (1.0, 100.0)
    elif any(char in var_lower for char in ['m', 'mass']):
        return (0.01, 5.0)
    elif any(char in var_lower for char in ['omega', '\u03c9', 'angular']):
        return (1.0, 20.0)
    elif any(char in var_lower for char in ['dbdt', 'alpha', 'rate']):
        return (0.1, 10.0)
    elif any(char in var_lower for char in ['density']):
        return (1000.0, 10000.0)
    else:
        return (0.5, 20.0)


def is_answer_reasonable_dynamic(correct_answers, var_values, attempt_num, constraints=None):
    if not correct_answers:
        return False

    constraints = constraints or {}
    min_answer = constraints.get('min_answer')
    max_answer = constraints.get('max_answer')
    non_negative = constraints.get('non_negative', False)

    for answer in correct_answers:
        if not isinstance(answer, (int, float)) or not np.isfinite(answer):
            return False

        if non_negative and answer < 0:
            return False

        abs_answer = abs(answer)

        max_threshold = 1e6 * (1 + attempt_num * 0.2)
        min_threshold = 1e-8 / (1 + attempt_num * 0.2)

        if max_answer is not None:
            max_threshold = min(max_threshold, max_answer)
        if min_answer is not None:
            min_threshold = max(min_threshold, min_answer)

        if abs_answer > max_threshold or (0 < abs_answer < min_threshold):
            return False

        if not check_dynamic_consistency(answer, var_values, attempt_num):
            return False

    return True


def check_dynamic_consistency(answer, var_values, attempt_num):
    if not var_values:
        return True

    abs_answer = abs(answer)
    if abs_answer == 0:
        return True
    var_values_list = [abs(v) for v in var_values.values() if isinstance(v, (int, float))]

    if not var_values_list:
        return True

    avg_var = sum(var_values_list) / len(var_values_list)

    base_max_ratio = 1000
    base_min_ratio = 0.001
    relaxation_factor = 1 + (attempt_num * 0.3)

    max_ratio = base_max_ratio * relaxation_factor
    min_ratio = base_min_ratio / relaxation_factor

    ratio_to_avg = abs_answer / avg_var if avg_var > 0 else abs_answer

    if ratio_to_avg > max_ratio or ratio_to_avg < min_ratio:
        return False

    return True


def format_problem_text(problem_text, var_values):
    def replace_var(match):
        var_name = match.group(1) or match.group(2)
        return str(var_values.get(var_name, match.group(0)))

    pattern = r'\{\{\s*(?:problem\.var_values\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\}\}|__([A-Za-z_][A-Za-z0-9_]*)__'
    return normalize_problem_image_urls(re.sub(pattern, replace_var, problem_text))


def normalize_problem_image_urls(problem_text):
    if not problem_text:
        return problem_text

    def replace_static_image(match):
        quote_char = match.group(1)
        filename = unquote(match.group(2).split('?', 1)[0])
        return f'src={quote_char}/problem_image/{quote(filename)}{quote_char}'

    return re.sub(
        r'src=(["\'])/static/images/([^"\']+)\1',
        replace_static_image,
        problem_text
    )


def infer_variable_units(problem_text, variables):
    units = {}
    if not problem_text:
        return units

    for var in variables:
        escaped_var = re.escape(var)
        placeholder_pattern = (
            r'(?:\{\{\s*(?:problem\.var_values\.)?' + escaped_var + r'\s*\}\}|__' + escaped_var + r'__)'
        )
        match = re.search(placeholder_pattern + r'\s*(?:\\,\s*)?(?:\\text\{([^}]+)\}|([A-Za-zμΩ°/%·^0-9\-/]+))', problem_text)
        if match:
            unit = (match.group(1) or match.group(2) or '').strip()
            unit = unit.replace('\\Omega', 'Ω').replace('\\mu', 'μ')
            if unit:
                units[var] = unit

    return units


def normalize_problem_placeholders(problem_text):
    if not problem_text:
        return problem_text
    problem_text = re.sub(
        r'\{\{\s*problem\.var_values\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}',
        r'{{\1}}',
        problem_text
    )
    return re.sub(r'__([A-Za-z_][A-Za-z0-9_]*)__', r'{{\1}}', problem_text)


def normalize_variable_specs(variables_text):
    variables, ranges = parse_variable_specs(variables_text or '')
    ordered = []
    seen = set()
    for var in variables:
        clean = var.strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        ordered.append(clean)

    parts = []
    for var in ordered:
        if var in ranges:
            min_val, max_val = ranges[var]
            parts.append(f"{var}[{min_val:g},{max_val:g}]")
        else:
            parts.append(var)
    return ','.join(parts)


def parse_variable_specs(variables_text):
    if not variables_text:
        return [], {}

    tokens = []
    current = []
    bracket_depth = 0
    for ch in variables_text:
        if ch == '[':
            bracket_depth += 1
        elif ch == ']':
            bracket_depth = max(0, bracket_depth - 1)

        if ch == ',' and bracket_depth == 0:
            token = ''.join(current).strip()
            if token:
                tokens.append(token)
            current = []
            continue

        current.append(ch)

    tail = ''.join(current).strip()
    if tail:
        tokens.append(tail)

    variables = []
    ranges = {}
    for token in tokens:
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]$', token)
        if match:
            name = match.group(1)
            min_val = float(match.group(2))
            max_val = float(match.group(3))
            if min_val > max_val:
                min_val, max_val = max_val, min_val
            variables.append(name)
            ranges[name] = (min_val, max_val)
        else:
            variables.append(token)

    return variables, ranges


def parse_answer_units(template):
    if template.get('answer_units'):
        return [u.strip() for u in template['answer_units'].split(',')]
    return []


def infer_answer_constraints(answer_units):
    merged_units = ' '.join(answer_units).lower()
    non_negative_units = ['kg', 'j', 'n', 'pa', 'w', 'hz', '\u03c9', 'ohm', '\u03a9']
    non_negative = any(unit.lower() in merged_units for unit in non_negative_units)

    return {
        'non_negative': non_negative,
        'min_answer': 0 if non_negative else None,
        'max_answer': 1e7
    }
