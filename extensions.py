import logging
import fnmatch
import threading
import time

from flask import Flask

from config import PERMANENT_SESSION_LIFETIME, REDIS_URL, SECRET_KEY, UPLOAD_FOLDER

app = Flask(__name__, template_folder='templates', static_folder='static', static_url_path='/static')
app.secret_key = SECRET_KEY
app.config['PERMANENT_SESSION_LIFETIME'] = PERMANENT_SESSION_LIFETIME
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

logger = logging.getLogger(__name__)


class InMemoryRedis:
    """Small Redis-compatible fallback for local development without Redis."""

    def __init__(self):
        self._values = {}
        self._lists = {}
        self._expires = {}
        self._lock = threading.RLock()

    def _cleanup_key(self, key):
        expires_at = self._expires.get(key)
        if expires_at is not None and expires_at <= time.time():
            self._values.pop(key, None)
            self._lists.pop(key, None)
            self._expires.pop(key, None)
            return True
        return False

    def setex(self, key, ttl, value):
        with self._lock:
            self._values[key] = value
            self._expires[key] = time.time() + int(ttl)
            self._lists.pop(key, None)
        return True

    def get(self, key):
        with self._lock:
            if self._cleanup_key(key):
                return None
            return self._values.get(key)

    def expire(self, key, ttl):
        with self._lock:
            if key not in self._values and key not in self._lists:
                return False
            self._expires[key] = time.time() + int(ttl)
        return True

    def lpush(self, key, value):
        with self._lock:
            if self._cleanup_key(key):
                pass
            self._lists.setdefault(key, []).insert(0, value)
            self._values.pop(key, None)
            return len(self._lists[key])

    def rpop(self, key):
        with self._lock:
            if self._cleanup_key(key):
                return None
            values = self._lists.get(key) or []
            if not values:
                return None
            return values.pop()

    def llen(self, key):
        with self._lock:
            if self._cleanup_key(key):
                return 0
            return len(self._lists.get(key) or [])

    def delete(self, *keys):
        deleted = 0
        with self._lock:
            for key in keys:
                existed = key in self._values or key in self._lists
                self._values.pop(key, None)
                self._lists.pop(key, None)
                self._expires.pop(key, None)
                if existed:
                    deleted += 1
        return deleted

    def scan_iter(self, match=None, count=None):
        with self._lock:
            for key in list(self._values.keys()) + list(self._lists.keys()):
                self._cleanup_key(key)
            keys = set(self._values.keys()) | set(self._lists.keys())
            matched_keys = [
                key for key in keys
                if match is None or fnmatch.fnmatch(key, match)
            ]
        return iter(matched_keys)


try:
    import redis
    RedisError = redis.RedisError
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
    CACHE_BACKEND = 'redis'
except Exception as err:
    RedisError = Exception
    redis_client = InMemoryRedis()
    CACHE_BACKEND = 'memory'
    logger.warning("Redis unavailable, using in-memory cache: %s", err)
