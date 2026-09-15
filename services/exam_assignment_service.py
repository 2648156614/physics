import hashlib
import hmac


def select_exam_template_ids(template_ids, question_count, exam_id, user_id, secret_key):
    """Return a stable, student-specific random-looking template order."""
    unique_ids = sorted({int(template_id) for template_id in template_ids})
    if not unique_ids:
        return []

    count = max(0, min(int(question_count), len(unique_ids)))
    key = str(secret_key or 'exam-assignment').encode('utf-8')

    def score(template_id):
        message = f'{int(exam_id)}:{int(user_id)}:{template_id}'.encode('utf-8')
        return hmac.new(key, message, hashlib.sha256).digest()

    return sorted(unique_ids, key=score)[:count]
