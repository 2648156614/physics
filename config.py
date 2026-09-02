import os
from datetime import timedelta

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))


def get_required_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


SECRET_KEY = get_required_env('SECRET_KEY')

SESSION_IDLE_TIMEOUT_SECONDS = 60 * 60
PERMANENT_SESSION_LIFETIME = timedelta(seconds=SESSION_IDLE_TIMEOUT_SECONDS)

MAX_LOGIN_FAILURES = 10
LOGIN_LOCK_MINUTES = 5
LOGIN_AUDIT_RETENTION_DAYS = 183
LOGIN_AUDIT_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
LOGIN_AUDIT_QUEUE_MAXSIZE = int(os.getenv('LOGIN_AUDIT_QUEUE_MAXSIZE', 20000))
LOGIN_AUDIT_BATCH_SIZE = int(os.getenv('LOGIN_AUDIT_BATCH_SIZE', 200))
LOGIN_AUDIT_FLUSH_INTERVAL_SECONDS = float(os.getenv('LOGIN_AUDIT_FLUSH_INTERVAL_SECONDS', 0.5))
UPGRADE_LEGACY_PASSWORD_ON_LOGIN = os.getenv('UPGRADE_LEGACY_PASSWORD_ON_LOGIN', 'false').lower() in ('1', 'true', 'yes', 'on')

REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
POOL_TARGET = int(os.getenv('POOL_TARGET', 50))
POOL_LOW_WATER = int(os.getenv('POOL_LOW_WATER', 25))
POOL_REFILL_BATCH = int(os.getenv('POOL_REFILL_BATCH', 10))
PROBLEM_TTL_SECONDS = int(os.getenv('PROBLEM_TTL_SECONDS', 900))
PROBLEM_POOL_TARGET_SIZE = int(os.getenv('PROBLEM_POOL_TARGET_SIZE', 20))
PROBLEM_POOL_REFILL_BATCH = int(os.getenv('PROBLEM_POOL_REFILL_BATCH', 10))

DB_CONFIG = {
    'host': os.getenv('MYSQL_HOST', 'localhost'),
    'port': int(os.getenv('MYSQL_PORT', 3306)),
    'user': os.getenv('MYSQL_USER', 'root'),
    'password': os.getenv('MYSQL_PASSWORD', ''),
    'database': os.getenv('MYSQL_DATABASE', 'physics_new3')
}
DB_POOL_NAME = os.getenv('DB_POOL_NAME', 'physics_app_pool')
MYSQL_CONNECTOR_POOL_MAX_SIZE = 32
DB_POOL_SIZE = int(os.getenv('DB_POOL_SIZE', 32))
DB_POOL_RESET_SESSION = os.getenv('DB_POOL_RESET_SESSION', 'true').lower() in ('1', 'true', 'yes', 'on')

UPLOAD_FOLDER = 'static/images'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'svg'}
MAX_FILE_SIZE = 16 * 1024 * 1024

AVATAR_FOLDER = 'static/avatars'
AVATAR_ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.svg'}
DEFAULT_AVATAR = 'default.svg'

DEFAULT_EXAM_PAPER_NAME = '默认题库'
