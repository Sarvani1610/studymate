"""Per user daily token budget.

The counter lives in Redis keyed by user and UTC date, so it resets on its own
and costs one INCRBY per LLM call. Admins are not limited.
"""
import logging
from datetime import UTC, datetime

import redis
from flask import current_app

from ..errors import QuotaExceeded
from ..extensions import get_redis

log = logging.getLogger(__name__)


def _key(user_id):
    return f"quota:tokens:{user_id}:{datetime.now(UTC):%Y%m%d}"


def used_today(user_id):
    try:
        return int(get_redis().get(_key(user_id)) or 0)
    except redis.RedisError:
        return 0


def ensure_budget(user, estimated=0):
    if user.is_admin:
        return
    budget = current_app.config["DAILY_TOKEN_BUDGET"]
    used = used_today(user.id)
    if used + estimated > budget:
        raise QuotaExceeded(
            "Daily AI usage limit reached. It resets at midnight UTC.",
            details={"used": used, "budget": budget},
        )


def record(user_id, tokens):
    if tokens <= 0:
        return
    try:
        client = get_redis()
        key = _key(user_id)
        pipe = client.pipeline()
        pipe.incrby(key, int(tokens))
        pipe.expire(key, 60 * 60 * 26)
        pipe.execute()
    except redis.RedisError as exc:
        log.warning("could not record token usage: %s", exc)


def summary(user):
    budget = current_app.config["DAILY_TOKEN_BUDGET"]
    used = used_today(user.id)
    return {"used": used, "budget": budget, "remaining": max(0, budget - used)}
