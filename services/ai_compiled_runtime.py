"""Signed, precomputed cases only. No solver and no formula execution at runtime."""
import hashlib
import hmac
import json
import os
import random

from services.ai_template_compiler import CompileError, render_case, require


def signature(template, key=None):
    secret = key if key is not None else os.getenv('SECRET_KEY', '')
    require(bool(secret), '缺少模板签名密钥')
    clean = json.loads(json.dumps(template))
    clean['generation_strategy'].pop('compiled_signature', None)
    payload = json.dumps(clean, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def canonical(database_template, strategy):
    return {'name': database_template['template_name'], 'text': database_template['problem_text'],
            'variables': database_template['variables'], 'formula': database_template['solution_formula'],
            'answer_count': database_template['answer_count'], 'answer_units': database_template['answer_units'],
            'image_filename': database_template.get('image_filename') or '', 'generation_strategy': strategy}


def generate_compiled(database_template, strategy):
    template = canonical(database_template, strategy)
    require(strategy.get('compiled_version') == 1 and strategy.get('strict') is True, '编译版本无效')
    stored = strategy.get('compiled_signature', '')
    require(isinstance(stored, str) and hmac.compare_digest(stored, signature(template)), '模板已变化或签名无效，请重新编译入库')
    cases = strategy.get('valid_cases')
    require(isinstance(cases, list) and 30 <= len(cases) <= 150, '缺少有效编译组合')
    result = render_case(template, random.choice(cases))
    result['template_id'] = database_template['id']
    # Do not expose hidden unit metadata in the student-facing parameter table.
    result['var_units'] = {name: unit for name, unit in result['var_units'].items() if name in result['var_values']}
    return result
