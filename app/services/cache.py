"""Redis caching helpers.

Course level caches (answers, search results) are invalidated with a version
counter instead of scanning keys: every key embeds the current course version,
and uploading or deleting a document bumps the version. Old keys just age out.
"""
import hashlib
import json
import logging

import redis

from ..extensions import get_redis
from ..metrics import CACHE_EVENTS

log = logging.getLogger(__name__)


def make_key(namespace, *parts):
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"cache:{namespace}:{digest}"


def get_json(key, cache_name="generic"):
    try:
        raw = get_redis().get(key)
    except redis.RedisError as exc:
        log.warning("cache read failed: %s", exc)
        CACHE_EVENTS.labels(cache_name, "error").inc()
        return None
    if raw is None:
        CACHE_EVENTS.labels(cache_name, "miss").inc()
        return None
    CACHE_EVENTS.labels(cache_name, "hit").inc()
    try:
        return json.loads(raw)
    except ValueError:
        return None


def set_json(key, value, ttl):
    try:
        get_redis().set(key, json.dumps(value, default=str), ex=int(ttl))
    except redis.RedisError as exc:
        log.warning("cache write failed: %s", exc)


def delete(*keys):
    if not keys:
        return
    try:
        get_redis().delete(*keys)
    except redis.RedisError as exc:
        log.warning("cache delete failed: %s", exc)


def course_version(course_id):
    try:
        value = get_redis().get(f"cache:course_ver:{course_id}")
        return int(value) if value else 0
    except redis.RedisError:
        return -1  # distinct value so a flaky Redis never serves stale data


def bump_course_version(course_id):
    try:
        return get_redis().incr(f"cache:course_ver:{course_id}")
    except redis.RedisError as exc:
        log.warning("could not bump course cache version: %s", exc)
        return None


def purge_namespace(namespace):
    """Admin helper. Uses SCAN so it does not block Redis on big keyspaces."""
    client = get_redis()
    removed = 0
    batch = []
    for key in client.scan_iter(match=f"cache:{namespace}:*", count=500):
        batch.append(key)
        if len(batch) >= 500:
            removed += client.delete(*batch)
            batch = []
    if batch:
        removed += client.delete(*batch)
    return removed
