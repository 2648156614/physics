import hashlib
import json
import re


def receipt_key(user_id, submission_id):
    if not isinstance(submission_id, str) or not re.fullmatch(r'[a-f0-9]{32}', submission_id):
        raise ValueError('无效的提交编号，请刷新页面后重试。')
    return f'exam:submission:{user_id}:{submission_id}'


def answer_fingerprint(payload, exam_id, problem_id):
    answers = {key: value for key, value in payload.items() if re.fullmatch(r'answer\d*', key)}
    return hashlib.sha256(json.dumps([exam_id, problem_id, answers], sort_keys=True).encode()).hexdigest()


def restore_receipt_state(receipt, session):
    current = session.get('current_problem') or {}
    result = receipt.get('current_problem') or {}
    # A late status response must never replace another exam, question or newly refreshed variant.
    if (result and all(current.get(key) == result.get(key) for key in ('exam_id', 'paper_id', 'display_number', 'user_id'))
            and current.get('token') in {receipt.get('source_token'), result.get('token')}):
        session['current_problem'] = result
