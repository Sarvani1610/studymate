import time

from flask import Blueprint, Response, current_app, jsonify
from sqlalchemy import text

from ..extensions import db, redis_available
from ..metrics import render_metrics

bp = Blueprint("health", __name__)
STARTED_AT = time.time()


@bp.get("/health/live")
def live():
    """Process is up. Used by the container health check; never touches dependencies."""
    return jsonify({"status": "ok"})


@bp.get("/health/ready")
def ready():
    """Dependencies are reachable. The load balancer only routes traffic here when this is 200."""
    checks = {}
    started = time.perf_counter()
    try:
        db.session.execute(text("SELECT 1"))
        checks["database"] = {"ok": True, "ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:
        db.session.rollback()
        checks["database"] = {"ok": False, "error": type(exc).__name__}

    started = time.perf_counter()
    redis_ok = redis_available()
    checks["redis"] = {"ok": redis_ok, "ms": round((time.perf_counter() - started) * 1000, 1)}

    healthy = all(c["ok"] for c in checks.values())
    body = {
        "status": "ok" if healthy else "degraded",
        "checks": checks,
        "uptime_s": int(time.time() - STARTED_AT),
        "version": current_app.config.get("APP_VERSION", "dev"),
    }
    return jsonify(body), 200 if healthy else 503


@bp.get("/metrics")
def metrics():
    payload, content_type = render_metrics()
    return Response(payload, mimetype=content_type.split(";")[0], headers={"Content-Type": content_type})


@bp.get("/api/version")
def version():
    cfg = current_app.config
    return jsonify({
        "name": cfg["APP_NAME"],
        "version": cfg.get("APP_VERSION", "dev"),
        "env": cfg["ENV_NAME"],
        "llm_provider": cfg["LLM_PROVIDER"],
        "embedding_provider": cfg["EMBEDDING_PROVIDER"],
    })
