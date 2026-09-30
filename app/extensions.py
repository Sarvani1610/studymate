"""Shared extension objects.

Kept in one module so blueprints and services can import them without pulling
in the application factory and causing circular imports.
"""
import logging

import redis
from flask import current_app
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)


def init_redis(app):
    if app.config.get("REDIS_FAKE"):
        import fakeredis

        client = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    else:
        client = redis.Redis.from_url(
            app.config["REDIS_URL"],
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
            health_check_interval=30,
            retry_on_timeout=True,
        )
    app.extensions["redis"] = client
    return client


def get_redis():
    return current_app.extensions["redis"]


def redis_available():
    try:
        return bool(get_redis().ping())
    except redis.RedisError as exc:
        log.warning("redis ping failed: %s", exc)
        return False
