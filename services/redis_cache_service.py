import json
import uuid

from config import PROBLEM_TTL_SECONDS, REDIS_LOCK_TTL_SECONDS
from extensions import RedisError, logger, redis_client


def get_pool_key(template_id):
    return f"exam:pool:{template_id}"


def get_problem_key(token):
    return f"exam:problem:{token}"


def acquire_distributed_lock(name, ttl_seconds=None):
    """Acquire a cross-instance Redis lock and return its owner token."""
    token = uuid.uuid4().hex
    ttl = max(1, int(ttl_seconds or REDIS_LOCK_TTL_SECONDS))
    acquired = redis_client.set(f"exam:lock:{name}", token, nx=True, ex=ttl)
    return token if acquired else None


def release_distributed_lock(name, token):
    """Release a lock only when it is still owned by this caller."""
    if not token:
        return False
    key = f"exam:lock:{name}"
    script = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('del', KEYS[1])
    end
    return 0
    """
    try:
        return bool(redis_client.eval(script, 1, key, token))
    except (AttributeError, NotImplementedError):
        if redis_client.get(key) == token:
            return bool(redis_client.delete(key))
        return False
    except RedisError as err:
        logger.warning("释放 Redis 分布式锁失败: name=%s error=%s", name, err)
        return False


def cache_problem_with_token(token, problem_data):
    """将题目数据写入 Redis 并设置 TTL。"""
    redis_client.setex(get_problem_key(token), PROBLEM_TTL_SECONDS, json.dumps(problem_data))


def get_problem_by_token(token):
    """通过 token 从 Redis 获取题目数据。"""
    if not token:
        return None
    raw = redis_client.get(get_problem_key(token))
    if not raw:
        return None
    try:
        redis_client.expire(get_problem_key(token), PROBLEM_TTL_SECONDS)
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def push_problem_to_pool(template_id, problem_data):
    redis_client.lpush(get_pool_key(template_id), json.dumps(problem_data))


def pop_problem_from_pool(template_id):
    raw_problem = redis_client.rpop(get_pool_key(template_id))
    if not raw_problem:
        return None
    try:
        return json.loads(raw_problem)
    except json.JSONDecodeError:
        return None


def get_pool_size(template_id):
    return redis_client.llen(get_pool_key(template_id))


def delete_problem_pool(template_id):
    return int(redis_client.delete(get_pool_key(template_id)) or 0)


def delete_problem_tokens_by_template(template_id):
    deleted_count = 0
    for key in redis_client.scan_iter(match="exam:problem:*", count=100):
        raw = redis_client.get(key)
        if not raw:
            continue
        try:
            data = json.loads(raw)
            cached_template_id = int(data.get("template_id", -1))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if cached_template_id == template_id:
            deleted_count += int(redis_client.delete(key) or 0)
    return deleted_count


def delete_cache_patterns(patterns):
    deleted_count = 0
    try:
        for pattern in patterns:
            keys = list(redis_client.scan_iter(match=pattern, count=100))
            if keys:
                deleted_count += int(redis_client.delete(*keys) or 0)
    except RedisError as err:
        logger.warning("批量清理 Redis 缓存失败: patterns=%s error=%s", patterns, err)
    return deleted_count
