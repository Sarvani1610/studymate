from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import func

from ..auth.decorators import admin_required, login_required
from ..errors import NotFound
from ..extensions import db
from ..models import Course, Document, UsageEvent, User
from ..services import cache, ingestion, quota
from ..services.analytics import platform_stats, user_stats
from ..utils.pagination import paginate
from ..utils.validation import Field, json_body
from .common import owned_course

bp = Blueprint("insights", __name__, url_prefix="/api")


@bp.get("/me/stats")
@login_required
def my_stats():
    course_id = request.args.get("course_id", type=int)
    if course_id:
        owned_course(course_id)
    return jsonify(user_stats(g.user, course_id))


@bp.get("/me/usage")
@login_required
def my_usage():
    rows = (
        db.session.query(
            UsageEvent.kind,
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.tokens_in + UsageEvent.tokens_out), 0),
        )
        .filter(UsageEvent.user_id == g.user.id)
        .group_by(UsageEvent.kind)
        .all()
    )
    return jsonify({
        "quota": quota.summary(g.user),
        "by_kind": [{"kind": k, "events": c, "tokens": int(t)} for k, c, t in rows],
    })


# ------------------------------------------------------------------ admin

@bp.get("/admin/stats")
@admin_required
def admin_stats():
    data = platform_stats()
    data["users"] = User.query.count()
    data["courses"] = Course.query.count()
    try:
        from ..tasks import celery

        inspector = celery.control.inspect(timeout=1.0)
        active = inspector.active() or {}
        data["workers"] = {name: len(tasks) for name, tasks in active.items()}
    except Exception:
        data["workers"] = None
    return jsonify(data)


@bp.get("/admin/users")
@admin_required
def admin_users():
    query = User.query.order_by(User.created_at.desc())
    q = request.args.get("q")
    if q:
        query = query.filter(User.email.ilike(f"%{q}%"))
    items, meta = paginate(query)
    return jsonify({"items": [{**u.to_dict(), "is_active": u.is_active} for u in items], "meta": meta})


@bp.patch("/admin/users/<int:user_id>")
@admin_required
def admin_update_user(user_id):
    user = db.session.get(User, user_id)
    if user is None:
        raise NotFound("User not found")
    data = json_body({"is_active": Field(bool), "role": Field(str, choices=("student", "admin"))}, partial=True)
    for key, value in data.items():
        setattr(user, key, value)
    db.session.commit()
    return jsonify({"user": {**user.to_dict(), "is_active": user.is_active}})


@bp.post("/admin/cache/purge")
@admin_required
def admin_purge_cache():
    data = json_body({
        "namespace": Field(str, required=True, choices=("answer", "search", "qemb", "topics", "stats")),
    })
    removed = cache.purge_namespace(data["namespace"])
    current_app.logger.info("admin purged cache namespace=%s removed=%s", data["namespace"], removed)
    return jsonify({"removed": removed})


@bp.post("/admin/courses/<int:course_id>/reindex")
@admin_required
def admin_reindex(course_id):
    course = db.session.get(Course, course_id)
    if course is None:
        raise NotFound("Course not found")
    queued = []
    for doc in Document.query.filter_by(course_id=course.id).all():
        doc.version += 1
        doc.status = "pending"
        queued.append(doc.id)
    db.session.commit()
    for doc_id in queued:
        ingestion.dispatch(doc_id)
    return jsonify({"queued": queued}), 202
