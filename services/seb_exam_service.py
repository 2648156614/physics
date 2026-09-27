import hashlib
import hmac
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
