from flask import Blueprint, g, jsonify, request

from ..auth.decorators import login_required
from ..errors import ValidationError
from ..extensions import db
from ..models import Flashcard, UsageEvent, utcnow
from ..services.generators import generate_flashcards, load_chunks
from ..services.rate_limit import rate_limit
from ..services.spaced_repetition import BUTTONS, apply_review, preview
from ..utils.pagination import paginate
from ..utils.validation import Field, json_body, query_int
from .common import owned_card, owned_course, validate_document_ids

bp = Blueprint("flashcards", __name__, url_prefix="/api")

GENERATE_SCHEMA = {
    "count": Field(int, default=10, min_value=1, max_value=40),
    "document_ids": Field(list, item_kind=int, max_items=50),
    "topics": Field(list, item_kind=str, max_items=10),
}
CARD_SCHEMA = {
    "front": Field(str, required=True, min_len=1, max_len=500),
    "back": Field(str, required=True, min_len=1, max_len=2000),
    "topic": Field(str, max_len=120),
    "suspended": Field(bool),
}


@bp.post("/courses/<int:course_id>/flashcards/generate")
@login_required
@rate_limit("llm")
def generate(course_id):
    owned_course(course_id)
    data = json_body(GENERATE_SCHEMA)
    doc_ids = validate_document_ids(course_id, data.get("document_ids"))
    chunks = load_chunks(course_id, document_ids=doc_ids, topics=data.get("topics"))
    drafts, tokens = generate_flashcards(g.user, chunks, count=data["count"])

    existing = {
        f.front.strip().lower()
        for f in Flashcard.query.filter_by(user_id=g.user.id, course_id=course_id).with_entities(Flashcard.front)
    }
    chunk_docs = {c.id: c.document_id for c in chunks}
    created = []
    for draft in drafts:
        if draft["front"].strip().lower() in existing:
            continue
        card = Flashcard(
            user_id=g.user.id,
            course_id=course_id,
            front=draft["front"],
            back=draft["back"],
            topic=(draft.get("topic") or "")[:120] or None,
            source_chunk_id=draft.get("chunk_id"),
            document_id=chunk_docs.get(draft.get("chunk_id")),
            due_at=utcnow(),
        )
        db.session.add(card)
        created.append(card)
        existing.add(draft["front"].strip().lower())
    db.session.add(UsageEvent(user_id=g.user.id, course_id=course_id, kind="flashcards", tokens_in=tokens))
    db.session.commit()
    return jsonify({"created": [c.to_dict() for c in created], "skipped_duplicates": len(drafts) - len(created)}), 201


@bp.get("/courses/<int:course_id>/flashcards")
@login_required
def list_cards(course_id):
    owned_course(course_id)
    query = Flashcard.query.filter_by(user_id=g.user.id, course_id=course_id)
    if request.args.get("due") == "true":
        query = query.filter(Flashcard.due_at <= utcnow(), Flashcard.suspended.is_(False))
    topic = request.args.get("topic")
    if topic:
        query = query.filter(Flashcard.topic == topic)
    items, meta = paginate(query.order_by(Flashcard.due_at))
    return jsonify({"items": [c.to_dict() for c in items], "meta": meta})


@bp.post("/courses/<int:course_id>/flashcards")
@login_required
def create_card(course_id):
    owned_course(course_id)
    data = json_body(CARD_SCHEMA)
    card = Flashcard(user_id=g.user.id, course_id=course_id, front=data["front"], back=data["back"],
                     topic=data.get("topic"), suspended=data.get("suspended", False), due_at=utcnow())
    db.session.add(card)
    db.session.commit()
    return jsonify({"flashcard": card.to_dict()}), 201


@bp.patch("/flashcards/<int:card_id>")
@login_required
def update_card(card_id):
    card = owned_card(card_id)
    for key, value in json_body(CARD_SCHEMA, partial=True).items():
        setattr(card, key, value)
    db.session.commit()
    return jsonify({"flashcard": card.to_dict()})


@bp.delete("/flashcards/<int:card_id>")
@login_required
def delete_card(card_id):
    db.session.delete(owned_card(card_id))
    db.session.commit()
    return "", 204


@bp.get("/flashcards/review")
@login_required
def review_queue():
    """Due cards, oldest first, with what each answer button would schedule."""
    limit = query_int("limit", 20, min_value=1, max_value=100)
    now = utcnow()
    query = Flashcard.query.filter(
        Flashcard.user_id == g.user.id, Flashcard.suspended.is_(False), Flashcard.due_at <= now
    )
    course_id = request.args.get("course_id", type=int)
    if course_id:
        owned_course(course_id)
        query = query.filter(Flashcard.course_id == course_id)
    total_due = query.count()
    cards = query.order_by(Flashcard.due_at).limit(limit).all()
    return jsonify({
        "total_due": total_due,
        "cards": [{**c.to_dict(), "buttons": preview(c, now)} for c in cards],
    })


@bp.post("/flashcards/<int:card_id>/review")
@login_required
def review(card_id):
    card = owned_card(card_id)
    data = json_body({
        "rating": Field(str, choices=tuple(BUTTONS)),
        "grade": Field(int, min_value=0, max_value=5),
    })
    if "grade" in data:
        grade = data["grade"]
    elif "rating" in data:
        grade = BUTTONS[data["rating"]]
    else:
        raise ValidationError("Send either rating (again, hard, good, easy) or grade (0 to 5)")
    apply_review(card, grade, utcnow())
    db.session.add(UsageEvent(user_id=g.user.id, course_id=card.course_id, kind="review"))
    db.session.commit()
    return jsonify({"flashcard": card.to_dict()})
