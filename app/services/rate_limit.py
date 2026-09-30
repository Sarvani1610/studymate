"""Sliding window rate limiter backed by Redis sorted sets.

Each request adds a member scored by its timestamp; members older than the
window are trimmed, and the remaining count is compared with the limit. This is
more accurate than fixed windows (no double burst at the boundary) and still
only costs one pipelined round trip per request.

If Redis is unreachable the limiter fails open and logs a warning. For a study
tool that is the better tradeoff than returning errors to every student.
"""
import logging
import time
import uuid
from functools import wraps

import jwt
import redis
from flask import current_app, g, request

from ..errors import RateLimited
from ..extensions import get_redis
from ..logging_setup import client_ip
from ..metrics import RATE_LIMITED

log = logging.getLogger(__name__)

PERIODS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}

SCOPE_CONFIG = {
    "default": "RATE_LIMIT_DEFAULT",
    "llm": "RATE_LIMIT_LLM",
    "auth": "RATE_LIMIT_AUTH",
    "upload": "RATE_LIMIT_UPLOAD",
}


def parse_limit(spec):
    try:
        count, period = spec.split("/")
        count = int(count.strip())
        period = period.strip().lower().rstrip("s")
        return count, PERIODS[period]
    except (ValueError, KeyError):
        raise ValueError(f"bad rate limit spec: {spec!r}")


def identity():
    """Rate limit per user when we can tell who it is, otherwise per client IP.

    The global limit runs before the auth decorator has loaded g.user, so the
    token's subject is read here directly. The signature is still verified;
    an invalid token just falls back to the IP.
    """
    user = getattr(g, "user", None)
    if user is not None:
        return f"user:{user.id}"
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        try:
            claims = jwt.decode(
                header.split(" ", 1)[1].strip(),
                current_app.config["SECRET_KEY"],
                algorithms=[current_app.config["JWT_ALGORITHM"]],
            )
            return f"user:{claims['sub']}"
        except (jwt.InvalidTokenError, KeyError):
            pass
    return f"ip:{client_ip()}"


def hit(key, limit, window):
    """Record one hit. Returns (allowed, remaining, reset_seconds)."""
    client = get_redis()
    now = time.time()
    member = f"{now:.6f}:{uuid.uuid4().hex[:6]}"
    pipe = client.pipeline()
    pipe.zremrangebyscore(key, 0, now - window)
    pipe.zadd(key, {member: now})
    pipe.zcard(key)
    pipe.zrange(key, 0, 0, withscores=True)
    pipe.expire(key, window + 1)
    _, _, count, oldest, _ = pipe.execute()

    if count > limit:
        client.zrem(key, member)
        oldest_ts = oldest[0][1] if oldest else now
        reset = max(1, int(oldest_ts + window - now) + 1)
        return False, 0, reset
    oldest_ts = oldest[0][1] if oldest else now
    return True, max(0, limit - count), max(1, int(oldest_ts + window - now))


def check(scope="default"):
    cfg = current_app.config
    if not cfg.get("RATELIMIT_ENABLED", True):
        return None
    limit, window = parse_limit(cfg[SCOPE_CONFIG.get(scope, "RATE_LIMIT_DEFAULT")])
    key = f"rl:{scope}:{identity()}"
    try:
        allowed, remaining, reset = hit(key, limit, window)
    except redis.RedisError as exc:
        log.warning("rate limiter unavailable, failing open: %s", exc)
        return None
    headers = {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(reset),
    }
    if not allowed:
        RATE_LIMITED.labels(scope).inc()
        headers["Retry-After"] = str(reset)
        raise RateLimited(
            f"Too many requests. Try again in {reset} seconds.",
            details={"scope": scope, "limit": limit, "window_seconds": window},
            headers=headers,
        )
    g.rate_limit_headers = headers
    return headers


def rate_limit(scope="default"):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            check(scope)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def install_default_limit(app, exempt_prefixes=("/health", "/metrics", "/static", "/api/auth")):
    # /api/auth has its own stricter "auth" scope, so it is not double counted here.
    @app.before_request
    def _default_limit():
        from flask import request

        if request.method == "OPTIONS" or request.path.startswith(exempt_prefixes):
            return None
        check("default")
        return None

    @app.after_request
    def _headers(response):
        for key, value in (getattr(g, "rate_limit_headers", None) or {}).items():
            response.headers.setdefault(key, value)
        return response
