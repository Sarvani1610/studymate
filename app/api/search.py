from flask import Blueprint, jsonify, request

from ..auth.decorators import login_required
from ..errors import ValidationError
from ..models import Chunk, Document
from ..services import cache
from ..services.keywords import extract_keyphrases
from ..services.rate_limit import rate_limit
from ..services.retrieval import retrieve
from ..utils.validation import query_int
from .common import owned_course, validate_document_ids

bp = Blueprint("search", __name__, url_prefix="/api")


def _doc_ids_arg():
    raw = request.args.get("document_ids", "").strip()
    if not raw:
        return None
    try:
        return [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        raise ValidationError("document_ids must be a comma separated list of integers")


@bp.get("/courses/<int:course_id>/search")
@login_required
@rate_limit("default")
def search(course_id):
    owned_course(course_id)
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        raise ValidationError("Query parameter 'q' must be at least 2 characters")
    k = query_int("k", 8, min_value=1, max_value=25)
    doc_ids = validate_document_ids(course_id, _doc_ids_arg())

    key = cache.make_key("search", course_id, cache.course_version(course_id), k, doc_ids, q.lower())
    hit = cache.get_json(key, "search")
    if hit is not None:
        return jsonify({**hit, "cached": True})

    hits, _ = retrieve(course_id, q, k=k, document_ids=doc_ids)
    payload = {"query": q, "results": [h.to_dict() for h in hits]}
    cache.set_json(key, payload, 600)
    return jsonify({**payload, "cached": False})


@bp.get("/courses/<int:course_id>/topics")
@login_required
def topics(course_id):
    owned_course(course_id)
    limit = query_int("limit", 25, min_value=5, max_value=60)
    key = cache.make_key("topics", course_id, cache.course_version(course_id), limit)
    hit = cache.get_json(key, "topics")
    if hit is not None:
        return jsonify(hit)
    texts = [
        row[0]
        for row in Chunk.query.join(Document)
        .filter(Chunk.course_id == course_id, Document.status == "ready")
        .with_entities(Chunk.text)
        .all()
    ]
    payload = {"topics": extract_keyphrases(texts, top_n=limit)}
    cache.set_json(key, payload, 3600)
    return jsonify(payload)
