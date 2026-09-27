import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import patch


def load_service_module():
    fake_config = types.ModuleType('config')
    fake_config.PROBLEM_TTL_SECONDS = 900
    fake_config.REDIS_LOCK_TTL_SECONDS = 300
    fake_extensions = types.ModuleType('extensions')
    fake_extensions.RedisError = RuntimeError
    fake_extensions.logger = types.SimpleNamespace(warning=lambda *args, **kwargs: None)
    fake_extensions.redis_client = None
    module_path = pathlib.Path(__file__).parents[1] / 'services' / 'redis_cache_service.py'
    spec = importlib.util.spec_from_file_location('redis_cache_service_under_test', module_path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'config': fake_config, 'extensions': fake_extensions}):
        spec.loader.exec_module(module)
    return module


redis_cache_service = load_service_module()


class FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        return int(self.values.pop(key, None) is not None)


class DistributedLockTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.patch = patch.object(redis_cache_service, 'redis_client', self.redis)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_second_owner_cannot_acquire_active_lock(self):
        first_token = redis_cache_service.acquire_distributed_lock('pool:1', 30)
        second_token = redis_cache_service.acquire_distributed_lock('pool:1', 30)

        self.assertTrue(first_token)
        self.assertIsNone(second_token)

    def test_only_owner_can_release_lock(self):
        token = redis_cache_service.acquire_distributed_lock('submit:1', 30)

        self.assertFalse(redis_cache_service.release_distributed_lock('submit:1', 'not-owner'))
        self.assertIsNotNone(self.redis.get('exam:lock:submit:1'))
        self.assertTrue(redis_cache_service.release_distributed_lock('submit:1', token))
        self.assertIsNone(self.redis.get('exam:lock:submit:1'))


if __name__ == '__main__':
    unittest.main()
