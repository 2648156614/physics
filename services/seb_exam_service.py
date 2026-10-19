import hashlib
import hmac
import json
from urllib.parse import urldefrag


SEB_CONFIG_KEY_HEADER = 'X-SafeExamBrowser-ConfigKeyHash'


def parse_checkbox(value):
    """Return a stable boolean for HTML form and environment values."""
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def normalize_config_key(value):
    value = str(value or '').strip().lower()
    if len(value) != 64 or any(char not in '0123456789abcdef' for char in value):
        return None
    return value


def previous_config_keys(value):
    """Read the explicitly retained keys, ignoring malformed stored values."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(key for item in value if (key := normalize_config_key(item))))


def rotate_config_keys(exam, new_key, retire_previous=False):
    if retire_previous:
        return '[]'
    retained = previous_config_keys(exam.get('seb_previous_config_keys'))
    old_key = normalize_config_key(exam.get('seb_config_key'))
    if old_key and old_key != new_key:
        retained.append(old_key)
    return json.dumps(list(dict.fromkeys(key for key in retained if key != new_key)))


def validate_exam_config_request(exam, absolute_url, received_hash):
    key = normalize_config_key(exam.get('seb_config_key'))
    if not key:
        return False, '该考试尚未完成安全浏览器配置，请联系监考教师。'
    keys = [key, *previous_config_keys(exam.get('seb_previous_config_keys'))]
    if not any(verify_config_key_request(absolute_url, item, received_hash) for item in keys):
        return False, '该考试必须使用教师指定的 Safe Exam Browser 配置进入。'
    return True, None


def calculate_config_key_request_hash(absolute_url, config_key):
    normalized_key = normalize_config_key(config_key)
    if not absolute_url or not normalized_key:
        return None
    url_without_fragment = urldefrag(str(absolute_url))[0]
    payload = f'{url_without_fragment}{normalized_key}'.encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def verify_config_key_request(absolute_url, config_key, received_hash):
    expected_hash = calculate_config_key_request_hash(absolute_url, config_key)
    candidate = str(received_hash or '').strip().rstrip(';').lower()
    if expected_hash is None or len(candidate) != 64:
        return False
    return hmac.compare_digest(expected_hash, candidate)


def build_finish_summary(completed_count, total_count):
    total = max(0, int(total_count or 0))
    completed = min(total, max(0, int(completed_count or 0)))
    return {
        'completed_count': completed,
        'incomplete_count': total - completed,
        'total_count': total,
        'all_completed': total > 0 and completed == total,
    }
