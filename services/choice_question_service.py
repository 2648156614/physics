"""Fixed choice questions, independent of numeric formula generation and tolerance."""
import html
import json
import re

CHOICE_TYPES = ('single_choice', 'multiple_choice')


def choice_definition(strategy):
    if isinstance(strategy, str):
        try:
            strategy = json.loads(strategy)
        except (ValueError, TypeError):
            return None
    if not isinstance(strategy, dict) or strategy.get('mode') != 'choice_v1':
        return None
    kind = strategy.get('question_type')
    options = strategy.get('options')
    answers = strategy.get('correct_ids')
    if kind not in CHOICE_TYPES or not isinstance(options, list) or not 2 <= len(options) <= 8:
        raise ValueError('选择题须包含 2～8 个选项')
    ids = [option.get('id') for option in options if isinstance(option, dict)]
    if any(type(i) is not int for i in ids) or ids != list(range(1, len(options) + 1)):
        raise ValueError('选项编号无效')
    for option in options:
        if not isinstance(option.get('text', ''), str) or not isinstance(option.get('image_filename', ''), str):
            raise ValueError('选项内容格式无效')
        if not option.get('text', '').strip() and not option.get('image_filename') and strategy.get('options_in_stem') is not True:
            raise ValueError('每个选项至少填写文字或上传图片')
        if len(option.get('text', '')) > 1000:
            raise ValueError('每个选项文字请控制在 1000 字以内')
        filename = option.get('image_filename', '')
        if filename and ('/' in filename or '\\' in filename or '..' in filename):
            raise ValueError('选项图片文件名无效')
    if not isinstance(answers, list) or not answers or any(type(i) is not int or i not in ids for i in answers) or len(set(answers)) != len(answers):
        raise ValueError('请选择有效的正确答案')
    if kind == 'single_choice' and len(answers) != 1:
        raise ValueError('单选题只能设置一个正确答案')
    return strategy


def validate_choice_stem(strategy, problem_text):
    definition = choice_definition(strategy)
    if definition and definition.get('options_in_stem'):
        text = re.sub(r'<[^>]+>', '', problem_text or '').replace('&nbsp;', '').strip()
        if not text and not re.search(r'<img\b', problem_text or '', re.I):
            raise ValueError('请填写完整题目文字或插入整题图片')


def encode_selection(ids):
    # At most eight fixed IDs: exact integer storage in the legacy numeric response
    # column, without changing historical rows or applying numeric tolerances.
    return sum(1 << (i - 1) for i in ids)


def selection_labels(value):
    return '、'.join(chr(65 + i) for i in range(8) if int(value) & (1 << i)) or '未选择'


def validate_selection(problem, submitted):
    options = problem['choice_options']
    valid = {o['id'] for o in options}
    if not isinstance(submitted, list) or not submitted or any(type(i) is not int or i not in valid for i in submitted) or len(submitted) != len(set(submitted)):
        raise ValueError('请选择有效选项后提交')
    if problem['question_type'] == 'single_choice' and len(submitted) != 1:
        raise ValueError('单选题只能选择一个选项')
    return submitted


def grade_selection(problem, submitted):
    validate_selection(problem, submitted)
    value = encode_selection(submitted)
    return value, value == problem['correct_answers'][0]


def generate_choice_problem(template, definition, calculation_version):
    validate_choice_stem(definition, template['problem_text'])
    options = definition['options']
    kind = definition['question_type']
    snapshot = f'<div class="choice-options-snapshot" data-choice-question="{kind}">'
    for option in options:
        snapshot += f'<p>{chr(64 + option["id"])}. {html.escape(option.get("text", ""))}'
        if option.get('image_filename'):
            snapshot += f'<img class="img-fluid" alt="选项图片" src="/problem_image/{html.escape(option["image_filename"], quote=True)}">'
        snapshot += '</p>'
    snapshot += '</div>'
    return dict(problem_text=f'<span data-choice-question="{kind}"></span>' + template['problem_text'] + snapshot, var_values={}, display_var_values={},
                correct_answers=[encode_selection(definition['correct_ids'])], answer_count=1, answer_units=[''],
                template_id=template['id'], template_name=template['template_name'],
                question_type=kind, choice_options=options, calculation_version=calculation_version)
