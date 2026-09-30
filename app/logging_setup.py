"""Structured logging and per-request context.

Each request gets an id (taken from X-Request-ID when the load balancer sets
one) that is attached to every log line and echoed back in the response.
"""
import json
import logging
import sys
import time
import uuid

from flask import g, has_request_context, request


class RequestContextFilter(logging.Filter):
    def filter(self, record):
        if has_request_context():
            record.request_id = getattr(g, "request_id", "-")
            user = getattr(g, "user", None)
            record.user_id = user.id if user is not None else None
            record.path = request.path
        else:
            record.request_id = getattr(record, "request_id", "-")
            record.user_id = getattr(record, "user_id", None)
            record.path = getattr(record, "path", None)
        return True


class JsonFormatter(logging.Formatter):
    RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
        "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
        "relativeCreated", "thread", "threadName", "processName", "process", "message",
        "taskName",
    }

    def format(self, record):
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self.RESERVED and not key.startswith("_") and value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(app):
    level = getattr(logging, str(app.config.get("LOG_LEVEL", "INFO")).upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    if app.config.get("LOG_JSON"):
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s")
        )
    handler.addFilter(RequestContextFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for noisy in ("urllib3", "botocore", "werkzeug", "httpx", "openai"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def install_request_hooks(app):
    access_log = logging.getLogger("studymate.access")
    slow_ms = app.config.get("SLOW_REQUEST_MS", 1500)

    @app.before_request
    def _start():
        g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        g.started_at = time.perf_counter()

    @app.after_request
    def _finish(response):
        started = getattr(g, "started_at", None)
        elapsed_ms = int((time.perf_counter() - started) * 1000) if started else -1
        response.headers["X-Request-ID"] = getattr(g, "request_id", "-")
        response.headers["X-Response-Time-ms"] = str(elapsed_ms)

        origins = app.config.get("CORS_ORIGINS", "*")
        response.headers.setdefault("Access-Control-Allow-Origin", origins)
        response.headers.setdefault("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Request-ID")
        response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")

        if request.path.startswith("/metrics") or request.path.startswith("/health/live"):
            return response
        extra = {
            "method": request.method,
            "status": response.status_code,
            "latency_ms": elapsed_ms,
            "remote_addr": client_ip(),
        }
        if elapsed_ms >= slow_ms:
            access_log.warning("slow request", extra=extra)
        else:
            access_log.info("request", extra=extra)

        from .metrics import observe_request

        observe_request(request.method, request.url_rule.rule if request.url_rule else "unmatched",
                        response.status_code, elapsed_ms / 1000.0)
        return response


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"
