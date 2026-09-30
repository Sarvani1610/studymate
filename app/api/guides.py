from datetime import date

from flask import Blueprint, Response, g, jsonify

from ..auth.decorators import login_required
from ..extensions import db
from ..models import StudyGuide
from ..services.rate_limit import rate_limit
from ..services.study_guide import generate_guide
from ..utils.pagination import paginate
from ..utils.validation import Field, json_body
from .common import owned_course, owned_guide, validate_document_ids

bp = Blueprint("guides", __name__, url_prefix="/api")

GUIDE_SCHEMA = {
    "exam_date": Field(date),
    "hours_per_day": Field(float, default=2.0, min_value=0.25, max_value=12),
    "focus_topics": Field(list, item_kind=str, max_items=10),
    "document_ids": Field(list, item_kind=int, max_items=50),
}


@bp.post("/courses/<int:course_id>/study-guides")
@login_required
@rate_limit("llm")
def create_guide(course_id):
    course = owned_course(course_id)
    data = json_body(GUIDE_SCHEMA)
    guide = generate_guide(
        g.user,
        course,
        exam_date=data.get("exam_date"),
        hours_per_day=data["hours_per_day"],
        focus_topics=[t.strip().lower() for t in data.get("focus_topics") or [] if t.strip()],
        document_ids=validate_document_ids(course_id, data.get("document_ids")),
    )
    return jsonify({"study_guide": guide.to_dict()}), 201


@bp.get("/courses/<int:course_id>/study-guides")
@login_required
def list_guides(course_id):
    owned_course(course_id)
    query = StudyGuide.query.filter_by(course_id=course_id, user_id=g.user.id).order_by(StudyGuide.created_at.desc())
    items, meta = paginate(query)
    data = []
    for guide in items:
        item = guide.to_dict()
        item.pop("content")
        data.append(item)
    return jsonify({"items": data, "meta": meta})


@bp.get("/study-guides/<int:guide_id>")
@login_required
def get_guide(guide_id):
    return jsonify({"study_guide": owned_guide(guide_id).to_dict()})


@bp.get("/study-guides/<int:guide_id>/markdown")
@login_required
def guide_markdown(guide_id):
    guide = owned_guide(guide_id)
    return Response(
        guide.content,
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="study-guide-{guide.id}.md"'},
    )


@bp.delete("/study-guides/<int:guide_id>")
@login_required
def delete_guide(guide_id):
    db.session.delete(owned_guide(guide_id))
    db.session.commit()
    return "", 204
