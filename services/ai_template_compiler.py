"""Strict, bounded authoring compiler. No AI expression is passed to eval/sympify.

Run symbolic work in a disposable process; examination reads only compiled cases.
"""
import ast
import hashlib
import itertools
import json
import math
import re
import subprocess
import sys
from fractions import Fraction
from html import escape
from html.parser import HTMLParser
from pathlib import Path

VERSION = 1
MAX_CANDIDATES = 2000
MAX_CASES = 150
FIELDS = {'name', 'text', 'variables', 'formula', 'answer_count', 'answer_units',
          'image_filename', 'generation_strategy'}
FUNCTIONS = {'sqrt': math.sqrt, 'sin': math.sin, 'cos': math.cos, 'abs': abs}
NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,29}$')


class CompileError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise CompileError(message)


def number(value):
    require(isinstance(value, (int, float)) and not isinstance(value, bool), '候选值必须是数值')
    require(math.isfinite(value) and abs(value) <= 1e9, '数值非有限实数或过大')
    return value


class Expression:
    def __init__(self, source, names):
        require(isinstance(source, str) and 0 < len(source) <= 800, '表达式长度不合法')
        try:
            self.tree = ast.parse(source, mode='eval').body
        except (SyntaxError, RecursionError) as exc:
            raise CompileError('表达式语法错误') from exc
        require(sum(1 for _ in ast.walk(self.tree)) <= 120, '表达式过于复杂')
        self.names = set()
        self._check(self.tree, set(names))

    def _check(self, node, names):
        if isinstance(node, ast.Constant):
            number(node.value)
        elif isinstance(node, ast.Name):
            require(node.id in names or node.id == 'pi', '未声明变量：' + node.id)
            if node.id != 'pi':
                self.names.add(node.id)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            self._check(node.operand, names)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
            self._check(node.left, names)
            self._check(node.right, names)
            if isinstance(node.op, ast.Pow):
                power = self._constant(node.right)
                require(power is not None and float(power).is_integer() and abs(power) <= 8,
                        '幂指数必须是 -8 到 8 的常整数')
        elif isinstance(node, ast.Call):
            require(isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS
                    and len(node.args) == 1 and not node.keywords, '只允许白名单单参数数学函数')
            self._check(node.args[0], names)
        else:
            raise CompileError('禁止属性、索引、字符串、任意调用及其他非数学语法')

    @staticmethod
    def _constant(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            value = Expression._constant(node.operand)
            return -value if value is not None else None
        return None

    def value(self, values, symbolic=False):
        if symbolic:
            import sympy as sp
            functions = {'sqrt': sp.sqrt, 'sin': sp.sin, 'cos': sp.cos, 'abs': sp.Abs}
        else:
            functions = FUNCTIONS

        def visit(node):
            if isinstance(node, ast.Constant):
                return sp.Rational(str(node.value)) if symbolic else node.value
            if isinstance(node, ast.Name):
                return (sp.pi if symbolic else math.pi) if node.id == 'pi' else values[node.id]
            if isinstance(node, ast.UnaryOp):
                value = visit(node.operand)
                return -value if isinstance(node.op, ast.USub) else value
            if isinstance(node, ast.Call):
                return functions[node.func.id](visit(node.args[0]))
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            return left ** right
        return visit(self.tree)

    def dimension(self, dimensions):
        zero = (Fraction(0),) * 7
        def visit(node):
            if isinstance(node, ast.Constant):
                return zero
            if isinstance(node, ast.Name):
                return zero if node.id == 'pi' else dimensions[node.id]
            if isinstance(node, ast.UnaryOp):
                return visit(node.operand)
            if isinstance(node, ast.Call):
                dim = visit(node.args[0])
                if node.func.id in ('sin', 'cos'):
                    require(dim == zero, '三角函数参数必须为弧度或无量纲量')
                    return zero
                return tuple(x / 2 for x in dim) if node.func.id == 'sqrt' else dim
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, (ast.Add, ast.Sub)):
                if self._constant(node.left) == 0:
                    return right
                if self._constant(node.right) == 0:
                    return left
                require(left == right, '加减项量纲不一致')
                return left
            if isinstance(node.op, ast.Pow):
                return tuple(x * self._constant(node.right) for x in left)
            sign = -1 if isinstance(node.op, ast.Div) else 1
            return tuple(a + sign * b for a, b in zip(left, right))
        return visit(self.tree)


def unit_dimension(unit):
    # m, kg, s, A, K, mol, cd; SI numeric inputs, no implicit cm->m conversion.
    base = {name: tuple(Fraction(int(i == j)) for i in range(7))
            for j, name in enumerate(('m', 'kg', 's', 'A', 'K', 'mol', 'cd'))}
    base.update({'N': (1, 1, -2, 0, 0, 0, 0), 'J': (2, 1, -2, 0, 0, 0, 0),
                 'W': (2, 1, -3, 0, 0, 0, 0), 'Pa': (-1, 1, -2, 0, 0, 0, 0),
                 'C': (0, 0, 1, 1, 0, 0, 0), 'V': (2, 1, -3, -1, 0, 0, 0),
                 'ohm': (2, 1, -3, -2, 0, 0, 0), 'T': (0, 1, -2, -1, 0, 0, 0),
                 'Hz': (0, 0, -1, 0, 0, 0, 0), 'rad': (0,) * 7})
    require(isinstance(unit, str) and len(unit) <= 80, '单位格式错误')
    unit = unit.strip().replace('²', '**2').replace('³', '**3').replace('^', '**').replace('Ω', 'ohm').replace('·', '*')
    if unit in ('', '-', '1'):
        return (Fraction(0),) * 7
    expression = Expression(unit, base)
    return expression.dimension(base)


class SafeHTML(HTMLParser):
    tags = {'div', 'p', 'span', 'h5', 'h6', 'strong', 'em', 'b', 'i', 'br', 'sub', 'sup', 'ul', 'ol', 'li'}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        require(tag in self.tags, '题干包含不允许的 HTML 标签：' + tag)
        require(all(key == 'class' for key, _ in attrs), '题干 HTML 仅允许 class 属性')
        classes = ' '.join(value or '' for key, value in attrs if key == 'class')
        require(re.fullmatch(r'[A-Za-z0-9 _-]*', classes) is not None, 'HTML class 不合法')
        self.parts.append('<' + tag + (' class="' + classes + '"' if classes else '') + '>')

    def handle_endtag(self, tag):
        require(tag in self.tags, '不允许的 HTML 标签')
        self.parts.append('</' + tag + '>')

    def handle_data(self, data):
        require(not re.search(r'\\(?:href|url|html|class|style|require)\b', data, re.I), '禁止公式中的外部链接和 HTML 扩展')
        self.parts.append(escape(data, quote=False))


def safe_html(text):
    require(isinstance(text, str) and 0 < len(text) <= 16000, '题干长度不合法')
    parser = SafeHTML()
    parser.feed(text)
    parser.close()
    return ''.join(parser.parts)


def split_formulas(source, names):
    require(isinstance(source, str) and len(source) <= 2400, '答案公式过长')
    try:
        root = ast.parse(source, mode='eval').body
    except (SyntaxError, RecursionError) as exc:
        raise CompileError('答案公式语法错误') from exc
    nodes = root.elts if isinstance(root, (ast.Tuple, ast.List)) else [root]
    require(1 <= len(nodes) <= 3, '只支持 1 到 3 个答案')
    return [Expression(ast.unparse(node), names) for node in nodes]


def finite_choices(spec):
    require(isinstance(spec, dict) and spec.get('type') == 'choice', '所有被抽取的参数必须提供有限 choice 候选集合')
    values = spec.get('values')
    require(isinstance(values, list) and 1 <= len(values) <= 100, '候选集合须包含 1 到 100 个数值')
    return list(dict.fromkeys(number(x) for x in values))


def fingerprint(template, analysis):
    raw = json.dumps({'template': template, 'analysis': analysis}, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def compile_template(template, analysis):
    import sympy as sp
    require(isinstance(template, dict) and set(template) == FIELDS, '模板必须且只能包含约定的八个字段')
    require(isinstance(analysis, dict), '缺少变量单位和物理约束资料')
    template = json.loads(json.dumps(template, allow_nan=False))
    require(isinstance(template['name'], str) and 0 < len(template['name']) <= 100, '题名长度不合法')
    require(template['image_filename'] == '', '图片文件名必须由服务器管理')
    template['text'] = safe_html(template['text'])
    variables = {}
    pattern = r'([A-Za-z][A-Za-z0-9_]{0,29})\[\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\s*\]'
    raw = template['variables']
    require(isinstance(raw, str) and len(raw) <= 1000, '变量列表格式错误')
    matches = list(re.finditer(pattern, raw))
    require(re.sub(pattern, '', raw).replace(',', '').strip() == '' and matches, '每个可见变量必须声明明确范围')
    for match in matches:
        name, low, high = match.groups()
        require(name not in variables and name not in FUNCTIONS and name != 'pi', '变量名重复或与函数冲突')
        bounds = (float(low), float(high))
        require(bounds[0] <= bounds[1] and all(abs(x) <= 1e9 for x in bounds), '变量范围不合法')
        variables[name] = bounds
    require(len(variables) <= 12, '可见变量最多 12 个')
    placeholders = set(re.findall(r'\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}', template['text']))
    require(placeholders == set(variables), '题干占位符必须与可见变量完全对应')
    require('{{' not in re.sub(r'\{\{\s*[A-Za-z][A-Za-z0-9_]*\s*\}\}', '', template['text']), '占位符格式错误')
    strategy = template['generation_strategy']
    require(isinstance(strategy, dict) and strategy.get('enabled') is True, '严格模板必须启用生成策略')
    require(set(strategy) <= {'enabled', 'mode', 'solve_for', 'hidden_vars', 'derived_vars', 'target_answer',
            'target_answers', 'key_vars', 'free_vars', 'beauty_constraints', 'max_attempts',
            'max_denominator', 'parameter_cases', 'valid_cases', 'compiled_version', 'compiled_hash'}, '生成策略包含未知字段')
    require(type(strategy.get('max_attempts', 20)) is int and 1 <= strategy.get('max_attempts', 20) <= 200, 'max_attempts 须为1到200')
    require(strategy.get('mode') in ('inverse_v1', 'derived_inverse_v1'), '不支持的生成模式')
    require(not {'valid_cases', 'compiled_version', 'compiled_hash'} & set(strategy), '不接受外部提供的编译结果')
    hidden = strategy.get('hidden_vars') or {}
    derived = strategy.get('derived_vars') or {}
    require(isinstance(hidden, dict) and isinstance(derived, dict) and len(hidden) <= 4, '隐藏变量声明错误')
    require(not set(hidden) & set(variables) and set(derived) <= set(variables), '隐藏或派生变量声明冲突')
    require(strategy['mode'] == 'derived_inverse_v1' or not (hidden or derived), '普通反推不允许隐藏和派生变量')
    bounds = dict(variables)
    for name, spec in hidden.items():
        require(NAME.fullmatch(name) and name not in FUNCTIONS and name != 'pi', '隐藏变量名不合法')
        pair = spec.get('range')
        require(isinstance(pair, list) and len(pair) == 2, '隐藏变量需要范围')
        bounds[name] = (number(pair[0]), number(pair[1]))
        require(pair[0] <= pair[1], '隐藏变量范围倒置')
        require(not re.search(r'(?<![A-Za-z0-9_])' + re.escape(name) + r'(?![A-Za-z0-9_])', template['text']), '隐藏变量出现在题干中')
    all_names = set(bounds)
    expressions = split_formulas(template['formula'], variables)
    count = template['answer_count']
    require(type(count) is int and count == len(expressions), 'answer_count 与公式数量不一致')
    require(isinstance(template['answer_units'], str), '答案单位必须是逗号分隔的字符串')
    units = [x.strip() for x in template['answer_units'].split(',')]
    require(len(units) == count, '答案单位数量不一致')
    unit_map = analysis.get('variable_units')
    if isinstance(unit_map, list):
        require(len({x['name'] for x in unit_map}) == len(unit_map), '变量单位重复')
        unit_map = {x['name']: x['unit'] for x in unit_map}
    require(isinstance(unit_map, dict) and set(unit_map) == all_names, '必须声明所有可见和隐藏变量的 SI 单位')
    dims = {name: unit_dimension(unit) for name, unit in unit_map.items()}
    derived_exprs = {name: Expression(expr, all_names) for name, expr in derived.items()}
    for name, expr in derived_exprs.items():
        require(expr.dimension(dims) == dims[name], '派生变量量纲不一致：' + name)
    for expr, unit in zip(expressions, units):
        actual_dimension, expected_dimension = expr.dimension(dims), unit_dimension(unit)
        is_zero = not expr.names and expr.value({}) == 0
        require(is_zero or actual_dimension == expected_dimension, '答案公式与答案单位量纲不一致')
    solve_for = strategy.get('solve_for')
    if isinstance(solve_for, str):
        solve_for = [solve_for]
    require(isinstance(solve_for, list) and 1 <= len(solve_for) <= 3
            and len(set(solve_for)) == len(solve_for) and set(solve_for) <= all_names - set(derived), '反推变量不合法')
    targets = strategy.get('target_answers')
    if not targets:
        targets = {'0': strategy.get('target_answer')}
    require(isinstance(targets, dict) and len(targets) == len(solve_for), '独立目标答案数必须与反推变量数一致')
    target_sets = {}
    for index, spec in targets.items():
        require(str(index).isdigit() and int(index) < count, '目标答案索引不合法，使用从 0 开始的索引')
        target_sets[int(index)] = finite_choices(spec)
        require(8 <= len(target_sets[int(index)]) <= 15, '每个独立目标答案须设置8到15种候选值')
    sampled = {}
    for field in ('key_vars', 'free_vars'):
        values = strategy.get(field) or {}
        require(isinstance(values, dict) and not set(values) & set(sampled), '抽样变量重复')
        sampled.update({name: finite_choices(spec) for name, spec in values.items()})
    for name, spec in hidden.items():
        if name not in solve_for:
            require(name not in sampled, '隐藏变量重复抽样')
            sampled[name] = finite_choices(spec)
    expected_sampled = all_names - set(derived) - set(solve_for)
    cases = strategy.get('parameter_cases') or []
    require(isinstance(cases, list) and len(cases) <= 100, '联合参数组格式错误')
    case_names = set(cases[0]) if cases else set()
    require(not case_names & set(sampled) and case_names | set(sampled) == expected_sampled, '每个非反推、非派生变量必须有且只有一个候选来源')
    for case in cases:
        require(isinstance(case, dict) and set(case) == case_names, '联合参数组字段不一致')
        for value in case.values():
            number(value)
    n_candidates = math.prod(map(len, sampled.values())) * math.prod(map(len, target_sets.values())) * max(1, len(cases))
    require(n_candidates <= MAX_CANDIDATES, '候选交叉组合超过 2000，必须缩小有限集合')
    symbols = {name: sp.Symbol(name, real=True, **({'positive': True} if bounds[name][0] > 0
                       else {'negative': True} if bounds[name][1] < 0 else {})) for name in all_names}
    expanded = dict(symbols)
    pending = dict(derived_exprs)
    order = []
    while pending:
        ready = [name for name, expr in pending.items() if not expr.names & set(pending)]
        require(ready, '派生变量循环依赖')
        for name in ready:
            expanded[name] = pending.pop(name).value(expanded, symbolic=True)
            order.append(name)
    symbolic_answers = [expr.value(expanded, symbolic=True) for expr in expressions]
    target_symbols = {index: sp.Dummy('target' + str(index), real=True,
                      **({'positive': True} if min(values) > 0 else {})) for index, values in target_sets.items()}
    equations = [symbolic_answers[index] - target_symbols[index] for index in target_sets]
    solve_symbols = [symbols[name] for name in solve_for]
    require(sp.Matrix(equations).jacobian(solve_symbols).rank() == len(solve_for), '目标答案并不相互独立，不能分别随机')
    solutions = sp.solve(equations, solve_symbols, dict=True, check=True)
    require(solutions and len(solutions) <= 8, '无法得到有限、明确的反推解')
    constraints = []
    for rule in analysis.get('constraints', []):
        require(rule.get('op') in ('<', '<=', '>', '>='), '物理约束比较符不合法')
        left, right = Expression(rule['left'], all_names), Expression(rule['right'], all_names)
        require(left.dimension(dims) == right.dimension(dims)
                or (not left.names and left.value({}) == 0)
                or (not right.names and right.value({}) == 0), '物理约束量纲不一致')
        constraints.append((left, rule['op'], right))
    require(len(constraints) <= 20, '物理约束过多')
    beauty = strategy.get('beauty_constraints') or {}
    places = beauty.get('max_decimal_places', 3)
    denominator = beauty.get('max_fraction_denominator', strategy.get('max_denominator', 4))
    numerator = beauty.get('max_fraction_numerator', 20)
    require(type(places) is int and 0 <= places <= 3 and type(denominator) is int and 1 <= denominator <= 4
            and type(numerator) is int and 1 <= numerator <= 20, '漂亮数限制超出允许范围')
    require(beauty.get('display_style', 'decimal_first') == 'decimal_first', '显示顺序必须为整数、小数、简单分数')

    def display(value):
        require(math.isfinite(value) and abs(value) <= 1e6, '变量数值过大或不是有限实数')
        require(value == 0 or abs(value) >= 0.001, '可见变量过小，不能通过舍入显示成零')
        if math.isclose(value, round(value), abs_tol=1e-9, rel_tol=0):
            return int(round(value))
        rounded = round(value, places)
        if math.isclose(value, rounded, abs_tol=1e-9, rel_tol=0):
            return rounded
        frac = Fraction(value).limit_denominator(denominator)
        require(abs(frac.numerator) <= numerator and math.isclose(value, float(frac), abs_tol=1e-9, rel_tol=0), '可见变量不是漂亮数')
        return str(frac)

    valid, failures, seen, target_seen = [], {}, set(), set()
    sample_names, target_indices = list(sampled), list(target_sets)
    products = itertools.product(*(list(sampled.values()) + list(target_sets.values()) + [cases or [{}]]))
    for combination in products:
        fixed = dict(zip(sample_names, combination[:len(sample_names)]))
        fixed.update(combination[-1])
        desired = dict(zip(target_indices, combination[len(sample_names):-1]))
        substitutions = {symbols[name]: sp.Rational(str(value)) for name, value in fixed.items()}
        substitutions.update({target_symbols[index]: sp.Rational(str(value)) for index, value in desired.items()})
        accepted = []
        for solution in solutions:
            try:
                require(all(symbol in solution for symbol in solve_symbols), '反推解不完整')
                values = dict(fixed)
                for name in solve_for:
                    solved = solution[symbols[name]].subs(substitutions)
                    require(solved.is_real is True and not solved.free_symbols, '反推解不是确定实数')
                    values[name] = float(solved)
                for name in order:
                    values[name] = float(derived_exprs[name].value(values))
                for name, value in values.items():
                    low, high = bounds[name]
                    require(math.isfinite(value)
                            and (low <= value or math.isclose(value, low, rel_tol=1e-12, abs_tol=0))
                            and (value <= high or math.isclose(value, high, rel_tol=1e-12, abs_tol=0)), '参数超出声明范围')
                    if dims[name] == unit_dimension('kg'):
                        require(value > 0, '质量必须大于零')
                shown = {name: display(values[name]) for name in variables}
                # Recompute from the exact numbers the student will see, never from unrounded hidden values.
                visible_values = {name: float(Fraction(str(value))) for name, value in shown.items()}
                require(all(bounds[name][0] <= value <= bounds[name][1] for name, value in visible_values.items()), '显示参数超出严格范围')
                values.update(visible_values)
                answers = [float(expr.value(visible_values)) for expr in expressions]
                require(all(math.isfinite(x) and abs(x) <= 1e9 for x in answers), '答案不是有限实数或过大')
                require(all(math.isclose(answers[index], value, rel_tol=1e-9, abs_tol=1e-9)
                            for index, value in desired.items()), '按显示参数重算与目标答案不一致')
                for left, op, right in constraints:
                    a, b = left.value(values), right.value(values)
                    require({'<': a < b, '<=': a <= b, '>': a > b, '>=': a >= b}[op], '违反物理条件约束')
                accepted.append({'var_values': shown, 'correct_answers': answers})
            except (ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
                reason = str(exc) if isinstance(exc, CompileError) else '候选组合计算异常'
                failures[reason] = failures.get(reason, 0) + 1
        # Never silently select a branch when multiple physically admissible roots exist.
        unique = {json.dumps(case, sort_keys=True): case for case in accepted}
        if len(unique) > 1:
            failures['存在多个合格反推解，需补充范围或物理条件'] = failures.get('存在多个合格反推解，需补充范围或物理条件', 0) + 1
            continue
        if not unique:
            continue
        case = next(iter(unique.values()))
        key = json.dumps(case['var_values'], sort_keys=True)
        if key not in seen:
            seen.add(key)
            valid.append(case)
            target_seen.add(tuple(desired[index] for index in target_indices))
    require(30 <= len(valid) <= MAX_CASES, f'有效不同组合为 {len(valid)}，要求 30 到 {MAX_CASES} 组')
    require(8 <= len(target_seen) <= 225, '有效独立目标答案组合不足 8 种或过多')
    require(all(len({round(case['correct_answers'][index], 8) for case in valid}) >= 8 for index in target_indices),
            '每个独立目标答案至少需要 8 种实际有效值')
    report = {'candidate_count': n_candidates, 'valid_count': len(valid), 'target_count': len(target_seen),
              'rejections': failures, 'exhaustive': True,
              'checks': ['安全语法', '占位符', '量纲', '严格范围', '答案数量', '目标重算', '漂亮数', '隐藏变量隔离'],
              'teacher_review': '请核对识题内容、物理模型、图示与考点；程序量纲检查不证明物理模型正确。'}
    strategy.update(compiled_version=VERSION, valid_cases=valid, strict=True,
                    compiled_hash=fingerprint(template, analysis), variable_units=unit_map)
    require(len(json.dumps(strategy, ensure_ascii=False).encode()) < 60000, '编译结果超过模板存储大小限制')
    return {'template': template, 'analysis': analysis, 'report': report}


def compile_isolated(template, analysis, timeout=25):
    payload = json.dumps({'template': template, 'analysis': analysis}, ensure_ascii=False, allow_nan=False)
    require(len(payload.encode()) <= 200000, '模板资料过大')
    try:
        result = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'services.ai_template_compiler'],
                                input=payload, text=True, encoding='utf-8', capture_output=True,
                                cwd=str(Path(__file__).resolve().parent.parent), timeout=timeout,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired as exc:
        raise CompileError('编译超过时限，请简化公式或候选集合') from exc
    require(result.returncode == 0, '编译进程异常退出')
    response = json.loads(result.stdout)
    require(response.get('ok'), response.get('error', '编译失败'))
    return response['result']


def render_case(template, case):
    text = re.sub(r'\{\{\s*([A-Za-z][A-Za-z0-9_]*)\s*\}\}',
                  lambda match: escape(str(case['var_values'][match.group(1)])), template['text'])
    return {'problem_text': text, 'var_values': case['var_values'],
            'correct_answers': case['correct_answers'], 'answer_count': template['answer_count'],
            'answer_units': [unit.strip() for unit in template['answer_units'].split(',')],
            'template_name': template['name'], 'image_filename': template.get('image_filename') or None,
            'var_units': template['generation_strategy'].get('variable_units', {})}


if __name__ == '__main__':
    try:
        if sys.platform != 'win32':
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (23, 24))
        data = json.loads(sys.stdin.read(200001))
        output = {'ok': True, 'result': compile_template(data['template'], data['analysis'])}
    except Exception as error:
        output = {'ok': False, 'error': str(error) if isinstance(error, CompileError) else '模板数据或计算不合法'}
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))
