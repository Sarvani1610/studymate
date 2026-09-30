from datetime import date

from flask import Blueprint, g, jsonify
from sqlalchemy.exc import IntegrityError

from ..auth.decorators import login_required
from ..errors import Conflict
from ..extensions import db
from ..models import Course
from ..services import cache
from ..utils.pagination import paginate
from ..utils.validation import Field, json_body
from .common import owned_course

bp = Blueprint("courses", __name__, url_prefix="/api/courses")

SCHEMA = {
    "name": Field(str, required=True, min_len=1, max_len=200),
    "code": Field(str, required=True, min_len=1, max_len=40),
    "description": Field(str, default="", max_len=4000),
    "exam_date": Field(date),
}


@bp.get("")
@login_required
def list_courses():
    query = Course.query.filter_by(owner_id=g.user.id).order_by(Course.created_at.desc())
    items, meta = paginate(query)
    return jsonify({"items": [c.to_dict(with_counts=True) for c in items], "meta": meta})


@bp.post("")
@login_required
def create_course():
    data = json_body(SCHEMA)
    data["code"] = data["code"].upper()
    course = Course(owner_id=g.user.id, **data)
    db.session.add(course)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise Conflict(f"You already have a course with code {data['code']}")
    return jsonify({"course": course.to_dict(with_counts=True)}), 201


@bp.get("/<int:course_id>")
@login_required
def get_course(course_id):
    course = owned_course(course_id)
    return jsonify({"course": course.to_dict(with_counts=True)})


@bp.patch("/<int:course_id>")
@login_required
def update_course(course_id):
    course = owned_course(course_id)
    data = json_body(SCHEMA, partial=True)
    if "code" in data:
        data["code"] = data["code"].upper()
    for key, value in data.items():
        setattr(course, key, value)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise Conflict("Another course already uses that code")
    return jsonify({"course": course.to_dict(with_counts=True)})


@bp.delete("/<int:course_id>")
@login_required
def delete_course(course_id):
    course = owned_course(course_id)
    from ..services.storage import get_storage

    storage = get_storage()
    keys = [d.storage_key for d in course.documents]
    db.session.delete(course)
    db.session.commit()
    for key in keys:
        try:
            storage.delete(key)
        except Exception:
            pass
    cache.bump_course_version(course_id)
    return "", 204
