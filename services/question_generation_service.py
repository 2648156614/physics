import ast
import json
import logging
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
from services.generation_errors import TemplateGenerationError


TEMPLATE_CACHE = {}
TEMPLATE_CACHE_TS = 0
CALCULATION_VERSION = 2
logger = logging.getLogger(__name__)


def _formula_tree(expression, allowed_names, depth=0):
    if depth > 3:
        raise TemplateGenerationError('公式字符串嵌套过深')
    try:
        tree = ast.parse(str(expression).strip(), mode='eval')
    except SyntaxError as error:
        raise TemplateGenerationError('公式语法不正确') from error
    missing = sorted({node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} - set(allowed_names))
    if missing:
        raise TemplateGenerationError('公式使用了未定义的名称：' + '、'.join(missing))
    # Legacy templates can return a quoted expression. Validate that expression too.
    def count(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return _formula_tree(node.value, allowed_names, depth + 1)[1]
        if isinstance(node, (ast.Tuple, ast.List)):
            counts = [count(item) for item in node.elts]
            return sum(counts) if all(item is not None for item in counts) else None
        return None if isinstance(node, (ast.Call, ast.IfExp)) else 1
    return tree, count(tree.body)


def validate_template_definition(variables_text, formula, answer_count=1, generation_strategy=None):
    """Static validation only: no AI call, random sampling, or symbolic solving."""
    if not isinstance(formula, str) or not formula.strip():
        raise TemplateGenerationError('公式必须是非空文本')
    variables, ranges = parse_variable_specs(variables_text or '')
    if len(set(variables)) != len(variables) or any(not name.isidentifier() for name in variables):
        raise TemplateGenerationError('变量名称无效或重复，请检查变量配置')
    if any(not all(np.isfinite(value) for value in bounds) for bounds in ranges.values()):
        raise TemplateGenerationError('变量范围必须为有限数值')
    try:
        parsed_count = int(answer_count)
        if isinstance(answer_count, bool) or float(answer_count) != parsed_count:
            raise ValueError('non-integer answer count')
        answer_count = parsed_count
    except (ValueError, TypeError, OverflowError) as error:
        raise TemplateGenerationError('答案数量必须为正整数') from error
    if answer_count < 1:
        raise TemplateGenerationError('答案数量必须为正整数')
    strategy = parse_generation_strategy({'generation_strategy': generation_strategy})
    if generation_strategy not in (None, '') and strategy is None:
        raise TemplateGenerationError('生成策略必须是有效的 JSON 对象')
    strategy = strategy or {}
    names = set(variables) | set(build_formula_context())
    for field in ('hidden_vars', 'derived_vars'):
        if strategy.get(field) is not None and not isinstance(strategy[field], dict):
            raise TemplateGenerationError(f'{field} 必须是对象')
    if strategy.get('enabled', True):
        names.update((strategy.get('hidden_vars') or {}).keys())
        names.update((strategy.get('derived_vars') or {}).keys())
    _, count = _formula_tree(formula, names)
    if count is not None and count != answer_count:
        raise TemplateGenerationError(f'公式返回 {count} 个答案，与设置的 {answer_count} 个空不一致')
    constraints = strategy.get('answer_constraints') or {}
    if not isinstance(constraints, dict):
        raise TemplateGenerationError('answer_constraints 必须是对象')
    per_answer = constraints.get('per_answer', [])
    if not isinstance(per_answer, list) or len(per_answer) > answer_count:
        raise TemplateGenerationError('per_answer 必须按答案顺序配置，数量不能超过答案空数')
    for rule in [constraints] + per_answer:
        if not isinstance(rule, dict):
            raise TemplateGenerationError('每个答案的约束必须是对象')
        if 'non_negative' in rule and not isinstance(rule['non_negative'], bool):
            raise TemplateGenerationError('non_negative 必须为 true 或 false')
        for key in ('min_answer', 'max_answer'):
            if rule.get(key) is not None and (not isinstance(rule[key], (int, float)) or not np.isfinite(rule[key])):
                raise TemplateGenerationError(f'{key} 必须是有限数值')
        if rule.get('min_answer') is not None and rule.get('max_answer') is not None and rule['min_answer'] > rule['max_answer']:
            raise TemplateGenerationError('答案约束的下限不能大于上限')
    conditions = strategy.get('variable_constraints') or []
    if not isinstance(conditions, list):
        raise TemplateGenerationError('variable_constraints 必须是条件列表')
    for condition in conditions:
        tree, _ = _formula_tree(condition, names)
        allowed = (ast.Expression, ast.Compare, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp,
                   ast.Not, ast.UAdd, ast.USub, ast.BinOp, ast.Add, ast.Sub, ast.Mult,
                   ast.Div, ast.Pow, ast.Mod, ast.Name, ast.Load, ast.Constant,
                   ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)
        if not all(isinstance(node, allowed) for node in ast.walk(tree)):
            raise TemplateGenerationError('变量条件只支持数值、变量、算术和比较表达式')
    return constraints


def _conditions_match(template, var_values):
    strategy = parse_generation_strategy(template) or {}
    context = build_formula_context()
    context.update(var_values)
    return all(bool(eval(condition, {'__builtins__': {}}, context))
               for condition in (strategy.get('variable_constraints') or []))


def _check_answers(template, answers, var_values, constraints):
    if len(answers) != int(template.get('answer_count') or 1):
        raise TemplateGenerationError('公式返回的答案数量与答案空数不一致')
    _, ranges = parse_variable_specs(template.get('variables') or '')
    for name, (minimum, maximum) in ranges.items():
        value = var_values.get(name)
        if not isinstance(value, (int, float)) or not np.isfinite(value) or not minimum <= value <= maximum:
            return False
    return is_answer_reasonable_dynamic(answers, var_values, 0, constraints) and _conditions_match(template, var_values)


def verify_cached_problem(problem):
    """Recalculate legacy cached answers with their original variables, never reroll."""
    if problem.get('calculation_version') == CALCULATION_VERSION:
        return problem
    template = get_template(problem.get('template_id'))
    if not template:
        raise TemplateGenerationError('缓存对应的题目模板不可用')
    constraints = validate_template_definition(template.get('variables'), template['solution_formula'],
                                               template.get('answer_count', 1), template.get('generation_strategy'))
    context = build_formula_context()
    values = problem.get('var_values') or {}
    context.update(values)
    try:
        answers = evaluate_solution_formula(template['solution_formula'], context)
        if not _check_answers(template, answers, values, constraints):
            raise TemplateGenerationError('缓存题目不满足模板明确规定的条件')
    except Exception as error:
        raise TemplateGenerationError(f'旧缓存题目无法通过验算：{error}') from error
    return dict(problem, correct_answers=answers, answer_count=int(template.get('answer_count') or 1),
                calculation_version=CALCULATION_VERSION)


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


def format_display_number(
    value,
    max_denominator=4,
    max_decimal_places=3,
    max_fraction_numerator=None,
):
    if not isinstance(value, (int, float)) or not np.isfinite(value):
        return value
    if 0 < abs(value) < 10 ** (-max_decimal_places):
        return value

    if abs(value - round(value)) < 1e-9:
        return int(round(value))

    rounded = round(value, max_decimal_places)
    if abs(value - rounded) < 1e-9:
        return rounded

    fraction = Fraction(float(value)).limit_denominator(max_denominator)
    numerator_is_allowed = (
        max_fraction_numerator is None
        or abs(fraction.numerator) <= max_fraction_numerator
    )
    if (
        abs(float(fraction) - value) < 1e-8
        and abs(fraction.denominator) > 1
        and numerator_is_allowed
    ):
        return f"{fraction.numerator}/{fraction.denominator}"
    return rounded


SAFE_SYMBOLIC_FUNCTIONS = {
    'sqrt': sp.sqrt,
    'sin': sp.sin,
    'cos': sp.cos,
    'tan': sp.tan,
    'asin': sp.asin,
    'acos': sp.acos,
    'atan': sp.atan,
    'exp': sp.exp,
    'log': sp.log,
    'ln': sp.log,
    'abs': sp.Abs,
    'Abs': sp.Abs,
    'min': sp.Min,
    'max': sp.Max,
}
SAFE_SYMBOLIC_CONSTANTS = {'pi': sp.pi, 'E': sp.E}
SAFE_SYMBOLIC_BINARY_OPERATORS = {
    ast.Add: lambda left, right: left + right,
    ast.Sub: lambda left, right: left - right,
    ast.Mult: lambda left, right: left * right,
    ast.Div: lambda left, right: left / right,
    ast.Pow: lambda left, right: left ** right,
}


def _build_safe_symbolic_node(node, symbols):
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError('only finite numeric constants are allowed')
        if not np.isfinite(value) or abs(value) > 1e12:
            raise ValueError('numeric constant is outside the allowed range')
        return sp.Integer(value) if isinstance(value, int) else sp.Rational(str(value))

    if isinstance(node, ast.Name):
        if node.id in symbols:
            return symbols[node.id]
        if node.id in SAFE_SYMBOLIC_CONSTANTS:
            return SAFE_SYMBOLIC_CONSTANTS[node.id]
        raise ValueError(f'undeclared symbol: {node.id}')

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = _build_safe_symbolic_node(node.operand, symbols)
        return operand if isinstance(node.op, ast.UAdd) else -operand

    if isinstance(node, ast.BinOp) and type(node.op) in SAFE_SYMBOLIC_BINARY_OPERATORS:
        left = _build_safe_symbolic_node(node.left, symbols)
        right = _build_safe_symbolic_node(node.right, symbols)
        if isinstance(node.op, ast.Pow):
            if right.free_symbols:
                raise ValueError('symbolic exponents are not allowed')
            exponent = float(sp.N(right))
            if not np.isfinite(exponent) or abs(exponent) > 12:
                raise ValueError('exponent is outside the allowed range')
        return SAFE_SYMBOLIC_BINARY_OPERATORS[type(node.op)](left, right)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in SAFE_SYMBOLIC_FUNCTIONS:
            raise ValueError('function is not allowed')
        if node.keywords:
            raise ValueError('keyword arguments are not allowed')
        arguments = [_build_safe_symbolic_node(argument, symbols) for argument in node.args]
        if not arguments:
            raise ValueError('function requires at least one argument')
        return SAFE_SYMBOLIC_FUNCTIONS[node.func.id](*arguments)

    raise ValueError(f'unsupported expression element: {type(node).__name__}')


def _parse_safe_symbolic_expressions(expression_text, symbols, allow_multiple=False):
    if not isinstance(expression_text, str) or not expression_text.strip():
        raise ValueError('expression must be a non-empty string')
    if len(expression_text) > 1000:
        raise ValueError('expression is too long')

    parsed = ast.parse(expression_text.strip(), mode='eval')
    if sum(1 for _ in ast.walk(parsed)) > 160:
        raise ValueError('expression is too complex')

    body = parsed.body
    if isinstance(body, (ast.Tuple, ast.List)):
        if not allow_multiple:
            raise ValueError('multiple expressions are not allowed here')
        nodes = body.elts
    else:
        nodes = [body]
    return [_build_safe_symbolic_node(node, symbols) for node in nodes]


def _expand_derived_expressions(derived_expressions, symbols):
    expanded = {}

    def expand(name, stack):
        if name in expanded:
            return expanded[name]
        if name in stack:
            raise ValueError('derived variable dependency cycle detected')
        expression = derived_expressions[name]
        if symbols[name] in expression.free_symbols:
            raise ValueError('derived variable cannot reference itself')
        substitutions = {}
        for dependency_name in derived_expressions:
            if dependency_name == name:
                continue
            dependency_symbol = symbols[dependency_name]
            if dependency_symbol in expression.free_symbols:
                substitutions[dependency_symbol] = expand(dependency_name, stack | {name})
        expanded[name] = sp.simplify(expression.subs(substitutions, simultaneous=True))
        return expanded[name]

    for derived_name in derived_expressions:
        expand(derived_name, set())
    return expanded


@lru_cache(maxsize=256)
def _prepare_derived_inverse_solver(
    solution_formula,
    variables,
    hidden_names,
    positive_hidden_names,
    derived_json,
    solve_for,
    target_indices,
):
    all_names = tuple(dict.fromkeys(tuple(variables) + tuple(hidden_names)))
    positive_hidden_names = set(positive_hidden_names)
    symbols = {
        name: (
            sp.symbols(name, positive=True)
            if name in positive_hidden_names
            else sp.symbols(name, real=True)
        )
        for name in all_names
    }
    solution_expressions = _parse_safe_symbolic_expressions(
        solution_formula,
        symbols,
        allow_multiple=True,
    )
    derived_specs = json.loads(derived_json)
    derived_expressions = {
        name: _parse_safe_symbolic_expressions(expression, symbols)[0]
        for name, expression in derived_specs.items()
    }
    expanded_derived = _expand_derived_expressions(derived_expressions, symbols)
    derived_substitutions = {
        symbols[name]: expression for name, expression in expanded_derived.items()
    }
    transformed_expressions = tuple(
        sp.simplify(expression.subs(derived_substitutions, simultaneous=True))
        for expression in solution_expressions
    )

    solve_symbols = [symbols[name] for name in solve_for]
    target_symbols = sp.symbols(f'_derived_target_0:{len(target_indices)}', real=True)
    equations = [
        sp.Eq(transformed_expressions[answer_index], target_symbol)
        for answer_index, target_symbol in zip(target_indices, target_symbols)
    ]
    solutions = sp.solve(equations, solve_symbols, dict=True)
    if isinstance(solutions, dict):
        solutions = [solutions]
    return (
        symbols,
        tuple(solution_expressions),
        tuple(sorted(expanded_derived.items())),
        tuple(solve_symbols),
        tuple(target_symbols),
        tuple(solutions or []),
    )


def _strict_range_contains(value, value_range):
    try:
        minimum, maximum = map(float, value_range)
    except (TypeError, ValueError):
        return False
    if minimum > maximum:
        minimum, maximum = maximum, minimum
    tolerance = max(1e-9, max(abs(minimum), abs(maximum)) * 1e-9)
    return minimum - tolerance <= value <= maximum + tolerance


def _is_valid_numeric_range(value_range):
    if not isinstance(value_range, (list, tuple)) or len(value_range) != 2:
        return False
    try:
        minimum, maximum = map(float, value_range)
    except (TypeError, ValueError):
        return False
    return np.isfinite(minimum) and np.isfinite(maximum) and minimum != maximum


def _is_display_friendly(
    value,
    max_denominator,
    max_decimal_places=3,
    max_fraction_numerator=None,
):
    if not isinstance(value, (int, float)) or not np.isfinite(value):
        return False
    if abs(value - round(value)) <= 1e-9:
        return True
    if abs(round(value, max_decimal_places) - value) <= 1e-9:
        return True
    fraction = Fraction(float(value)).limit_denominator(max_denominator)
    return (
        abs(float(fraction) - value) <= 1e-8
        and abs(fraction.denominator) > 1
        and (
            max_fraction_numerator is None
            or abs(fraction.numerator) <= max_fraction_numerator
        )
    )


def _problem_contains_hidden_placeholder(problem_text, hidden_names):
    for name in hidden_names:
        escaped_name = re.escape(name)
        if re.search(
            rf'\{{\{{\s*(?:problem\.var_values\.)?{escaped_name}\s*\}}\}}|__{escaped_name}__',
            problem_text or '',
        ):
            return True
    return False


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

        var_values.update({name: float(value) for name, value in solved_values.items()})
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


def generate_derived_inverse_problem(
    template,
    variables,
    configured_ranges,
    local_vars,
    answer_units,
    answer_constraints,
):
    strategy = parse_generation_strategy(template)
    if not strategy or not strategy.get('enabled', True) or strategy.get('mode') != 'derived_inverse_v1':
        return None

    hidden_vars = strategy.get('hidden_vars')
    derived_vars = strategy.get('derived_vars')
    key_vars = strategy.get('key_vars') or {}
    if not isinstance(hidden_vars, dict) or not isinstance(derived_vars, dict) or not isinstance(key_vars, dict):
        return None
    if not hidden_vars or not derived_vars or len(hidden_vars) > 4 or len(derived_vars) > 20:
        return None

    hidden_names = list(hidden_vars)
    derived_names = list(derived_vars)
    all_declared_names = set(variables) | set(hidden_names)
    if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name or '') for name in all_declared_names):
        return None
    if set(hidden_names).intersection(variables):
        return None
    if not set(derived_names).issubset(variables):
        return None
    if set(key_vars) - (set(variables) - set(derived_names)):
        return None
    if any(not isinstance(spec, dict) for spec in key_vars.values()):
        return None
    if any(name not in configured_ranges for name in variables):
        return None
    if any(
        not isinstance(hidden_vars.get(name), dict)
        or not _is_valid_numeric_range(hidden_vars[name].get('range'))
        for name in hidden_names
    ):
        return None
    if _problem_contains_hidden_placeholder(template.get('problem_text', ''), hidden_names):
        return None

    raw_solve_for = strategy.get('solve_for')
    if isinstance(raw_solve_for, str):
        solve_for = [raw_solve_for]
    elif isinstance(raw_solve_for, (list, tuple)):
        solve_for = [name for name in raw_solve_for if isinstance(name, str)]
    else:
        return None
    allowed_solve_names = set(hidden_names) | (
        set(variables) - set(derived_names)
    )
    if (
        not solve_for
        or len(set(solve_for)) != len(solve_for)
        or not set(solve_for).issubset(allowed_solve_names)
    ):
        return None

    sampled_hidden_names = [
        name for name in hidden_names if name not in solve_for
    ]
    solved_hidden_names = [
        name for name in solve_for if name in hidden_names
    ]
    solved_visible_names = [
        name for name in solve_for if name in variables
    ]

    answer_count = int(template.get('answer_count', 1) or 1)
    target_specs = _normalize_target_specs(strategy, answer_count)
    if len(target_specs) != len(solve_for):
        return None
    target_indices = tuple(sorted(target_specs))
    if any(index < 0 or index >= answer_count for index in target_indices):
        return None

    try:
        max_attempts = max(1, min(int(strategy.get('max_attempts', 50) or 50), 200))
        beauty_constraints = strategy.get('beauty_constraints') or {}
        if not isinstance(beauty_constraints, dict):
            return None
        if beauty_constraints.get('display_style', 'decimal_first') != 'decimal_first':
            return None
        max_decimal_places = max(
            0,
            min(int(beauty_constraints.get('max_decimal_places', 3)), 8),
        )
        max_denominator = max(
            1,
            min(
                int(
                    beauty_constraints.get(
                        'max_fraction_denominator',
                        strategy.get('max_denominator', 4),
                    )
                ),
                64,
            ),
        )
        max_fraction_numerator = max(
            1,
            min(int(beauty_constraints.get('max_fraction_numerator', 1000)), 100000),
        )
        derived_json = json.dumps(derived_vars, ensure_ascii=True, sort_keys=True)
        positive_hidden_names = tuple(
            name
            for name in hidden_names
            if min(map(float, hidden_vars[name]['range'])) > 0
        )
        (
            symbols,
            solution_expressions,
            expanded_derived_items,
            solve_symbols,
            target_symbols,
            solutions,
        ) = _prepare_derived_inverse_solver(
            template['solution_formula'],
            tuple(variables),
            tuple(hidden_names),
            positive_hidden_names,
            derived_json,
            tuple(solve_for),
            target_indices,
        )
    except Exception:
        return None
    if len(solution_expressions) != answer_count or not solutions:
        return None
    hidden_symbols = {symbols[name] for name in hidden_names}
    if any(expression.free_symbols.intersection(hidden_symbols) for expression in solution_expressions):
        return None

    expanded_derived = dict(expanded_derived_items)
    normalized_answer_units = list(answer_units[:answer_count])
    normalized_answer_units.extend([''] * (answer_count - len(normalized_answer_units)))
    ordinary_names = [name for name in variables if name not in derived_names]
    var_units = infer_variable_units(template.get('problem_text', ''), variables)

    def get_solve_range(name):
        if name in hidden_vars:
            return hidden_vars[name]['range']
        return configured_ranges[name]

    for attempt in range(max_attempts):
        try:
            target_answers = {
                answer_index: float(_rand_from_spec(spec, spec.get('range', (1, 50))))
                for answer_index, spec in target_specs.items()
            }
            if any(not np.isfinite(value) for value in target_answers.values()):
                continue

            hidden_values = {}
            for name in sampled_hidden_names:
                spec = hidden_vars[name]
                value = float(_rand_from_spec(spec, spec['range']))
                if not np.isfinite(value) or not _strict_range_contains(value, spec['range']):
                    raise ValueError('sampled hidden variable is outside its configured range')
                hidden_values[name] = value

            visible_values = {}
            for name in ordinary_names:
                if name in solved_visible_names:
                    continue
                value = float(_rand_from_spec(key_vars.get(name), configured_ranges[name]))
                if not np.isfinite(value) or not _strict_range_contains(value, configured_ranges[name]):
                    raise ValueError('ordinary variable is outside its configured range')
                visible_values[name] = value

            known_substitutions = {
                symbols[name]: value for name, value in visible_values.items()
            }
            known_substitutions.update({
                symbols[name]: value for name, value in hidden_values.items()
            })
            known_substitutions.update({
                target_symbol: target_answers[answer_index]
                for answer_index, target_symbol in zip(target_indices, target_symbols)
            })

            solved_values = None
            for solution in solutions:
                candidate_values = {}
                candidate_substitutions = dict(known_substitutions)
                unresolved = set(solve_for)
                for _ in solve_for:
                    made_progress = False
                    for name, solve_symbol in zip(solve_for, solve_symbols):
                        if name not in unresolved or solve_symbol not in solution:
                            continue
                        evaluated_expression = solution[solve_symbol].subs(candidate_substitutions)
                        if evaluated_expression.free_symbols:
                            continue
                        evaluated = sp.N(evaluated_expression)
                        if evaluated.is_real is False:
                            continue
                        value = float(evaluated)
                        if not np.isfinite(value):
                            continue
                        candidate_values[name] = value
                        candidate_substitutions[solve_symbol] = value
                        unresolved.remove(name)
                        made_progress = True
                    if not unresolved or not made_progress:
                        break
                if unresolved:
                    continue
                if all(
                    _strict_range_contains(candidate_values[name], get_solve_range(name))
                    for name in solve_for
                ):
                    solved_values = candidate_values
                    break
            if solved_values is None:
                continue

            for name, value in solved_values.items():
                if name in hidden_names:
                    hidden_values[name] = value
                else:
                    visible_values[name] = value
                known_substitutions[symbols[name]] = value

            expected_hidden_names = set(sampled_hidden_names) | set(solved_hidden_names)
            if set(hidden_values) != expected_hidden_names:
                continue

            for name, expression in expanded_derived.items():
                evaluated = sp.N(expression.subs(known_substitutions))
                if evaluated.free_symbols or evaluated.is_real is False:
                    raise ValueError('derived variable could not be reduced to a real number')
                value = float(evaluated)
                if not np.isfinite(value) or not _strict_range_contains(value, configured_ranges[name]):
                    raise ValueError('derived variable is outside its configured range')
                if name in {'R', 'T'} and value <= 0:
                    raise ValueError('physical variable must be positive')
                visible_values[name] = value
                known_substitutions[symbols[name]] = value

            if set(visible_values) != set(variables):
                continue
            if any(name in visible_values and visible_values[name] <= 0 for name in {'R', 'T'}):
                continue
            if any(
                not _is_display_friendly(
                    value,
                    max_denominator,
                    max_decimal_places,
                    max_fraction_numerator,
                )
                for value in visible_values.values()
            ):
                continue

            answer_substitutions = {
                symbols[name]: value for name, value in visible_values.items()
            }
            correct_answers = []
            for expression in solution_expressions:
                evaluated = sp.N(expression.subs(answer_substitutions))
                if evaluated.free_symbols or evaluated.is_real is False:
                    raise ValueError('answer could not be reduced to a real number')
                answer = float(evaluated)
                if not np.isfinite(answer):
                    raise ValueError('answer is not finite')
                correct_answers.append(answer)

            if not is_answer_reasonable_dynamic(
                correct_answers,
                visible_values,
                attempt,
                answer_constraints,
            ):
                continue
            if not all(
                abs(correct_answers[index] - target)
                <= max(1e-7, abs(target) * 1e-7)
                for index, target in target_answers.items()
            ):
                continue

            display_values = {
                name: format_display_number(
                    value,
                    max_denominator,
                    max_decimal_places,
                    max_fraction_numerator,
                )
                for name, value in visible_values.items()
            }
            if any(name in hidden_names for name in visible_values) or any(
                name in hidden_names for name in display_values
            ):
                continue
            problem_content = format_problem_text(template['problem_text'], display_values)
            return {
                'problem_text': problem_content,
                'var_values': {
                    name: float(visible_values[name]) for name in variables
                },
                'display_var_values': {
                    name: display_values[name] for name in variables
                },
                'var_units': var_units,
                'correct_answers': correct_answers,
                'answer_units': normalized_answer_units,
                'template_id': template['id'],
                'answer_count': answer_count,
                'template_name': template['template_name'],
                'image_filename': template.get('image_filename'),
                'generation_mode': 'derived_inverse_v1',
            }
        except (ArithmeticError, TypeError, ValueError, OverflowError):
            continue

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


def _sample_legacy_variables(variables, ranges, midpoint=False):
    values = {}
    for name in variables:
        minimum, maximum = ranges.get(name, get_adaptive_default_range(name))
        raw = (minimum + maximum) / 2 if midpoint else random.uniform(minimum, maximum)
        rounded = round(raw, 2)
        # Preserve ordinary two-decimal questions, but never erase small ranges/values.
        values[name] = (rounded if minimum <= rounded <= maximum and
                        (raw == 0 or abs(raw) >= 0.01) else raw)
    return values


def _generate_numeric_problem(template, variables, context, ranges, units, constraints, attempts, midpoint_last=False):
    last_error = '没有找到满足模板条件的参数'
    for attempt in range(attempts):
        values = _sample_legacy_variables(variables, ranges, midpoint_last and attempt == attempts - 1)
        try:
            current = dict(context, **values)
            answers = evaluate_solution_formula(template['solution_formula'], current)
            if not _check_answers(template, answers, values, constraints):
                last_error = '参数或答案不满足模板明确规定的条件'
                continue
            normalized_units = list(units[:int(template.get('answer_count') or 1)])
            normalized_units.extend([''] * (int(template.get('answer_count') or 1) - len(normalized_units)))
            return {
                'problem_text': format_problem_text(template['problem_text'], values),
                'var_values': values,
                'var_units': infer_variable_units(template.get('problem_text', ''), variables),
                'correct_answers': answers,
                'answer_units': normalized_units,
                'template_id': template['id'],
                'answer_count': int(template.get('answer_count') or 1),
                'template_name': template['template_name'],
                'image_filename': template.get('image_filename'),
                'calculation_version': CALCULATION_VERSION,
            }
        except TemplateGenerationError:
            raise
        except (NameError, SyntaxError) as error:
            raise TemplateGenerationError(f'公式无法计算：{error}') from error
        except Exception as error:
            last_error = str(error)
    logger.error('题目生成失败: template_id=%s reason=%s', template.get('id'), last_error)
    raise TemplateGenerationError(f'模板 {template.get("id")} 无法生成有效题目：{last_error}')


def generate_problem_from_template(template_id, max_attempts=10):
    template = get_template(template_id)
    if not template:
        return None
    constraints = validate_template_definition(template.get('variables'), template['solution_formula'],
                                               template.get('answer_count', 1), template.get('generation_strategy'))
    variables, configured_ranges = parse_variable_specs(template.get('variables', ''))
    context = build_formula_context()
    ranges = {name: configured_ranges.get(name, get_adaptive_default_range(name)) for name in variables}
    units = parse_answer_units(template)
    strategy = parse_generation_strategy(template) or {}
    mode = strategy.get('mode') if strategy.get('enabled', True) else None
    generator = {'derived_inverse_v1': generate_derived_inverse_problem,
                 'inverse_v1': generate_inverse_problem}.get(mode)
    if generator:
        result = generator(template, variables, configured_ranges, context, units[:], constraints)
        if result and _check_answers(template, result['correct_answers'], result['var_values'], constraints):
            result['calculation_version'] = CALCULATION_VERSION
            return result
    return _generate_numeric_problem(template, variables, context, ranges, units, constraints,
                                     max(1, int(max_attempts)))


def generate_fallback_problem(template, variables, local_vars, reasonable_ranges=None, answer_units=None, answer_constraints=None):
    # Compatibility entry point: all retries and the midpoint must pass the same checks.
    constraints = validate_template_definition(template.get('variables'), template['solution_formula'],
                                               template.get('answer_count', 1), template.get('generation_strategy'))
    if answer_constraints is not None:
        constraints = answer_constraints
    return _generate_numeric_problem(template, variables, local_vars, reasonable_ranges or {},
                                     answer_units or parse_answer_units(template), constraints, 6, midpoint_last=True)


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


def is_answer_reasonable_dynamic(correct_answers, var_values=None, attempt_num=0, constraints=None):
    """Accept finite signed answers of any magnitude; enforce explicit bounds only."""
    if not correct_answers:
        return False
    constraints = constraints or {}
    per_answer = constraints.get('per_answer') or []
    for index, answer in enumerate(correct_answers):
        if not isinstance(answer, (int, float)) or not np.isfinite(answer):
            return False
        rule = dict(constraints)
        if index < len(per_answer):
            rule.update(per_answer[index])
        if rule.get('non_negative') and answer < 0:
            return False
        if rule.get('min_answer') is not None and answer < rule['min_answer']:
            return False
        if rule.get('max_answer') is not None and answer > rule['max_answer']:
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
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\[\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*,\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*\]$', token)
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
    # Units alone do not determine sign or magnitude (work in J may be negative).
    return {}
