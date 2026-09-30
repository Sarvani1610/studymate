from flask import Blueprint, Response, g, jsonify, stream_with_context

from ..auth.decorators import login_required
from ..errors import NotFound, ValidationError
from ..extensions import db
from ..models import ChatSession, Document, Message
from ..services import rag
from ..services.rate_limit import rate_limit
from ..utils.pagination import paginate
from ..utils.validation import Field, json_body
from .common import owned_course, owned_session, validate_document_ids

bp = Blueprint("chat", __name__, url_prefix="/api")

SESSION_SCHEMA = {
    "title": Field(str, max_len=200),
    "document_ids": Field(list, item_kind=int, max_items=50),
}
QUESTION_SCHEMA = {"question": Field(str, required=True, min_len=2, max_len=2000)}


def _require_ready(course_id):
    ready = Document.query.filter_by(course_id=course_id, status="ready").count()
    if not ready:
        raise ValidationError("This course has no processed documents yet. Upload material first.")


@bp.get("/courses/<int:course_id>/sessions")
@login_required
def list_sessions(course_id):
    owned_course(course_id)
    query = ChatSession.query.filter_by(course_id=course_id, user_id=g.user.id).order_by(
        ChatSession.updated_at.desc()
    )
    items, meta = paginate(query)
    return jsonify({"items": [s.to_dict() for s in items], "meta": meta})


@bp.post("/courses/<int:course_id>/sessions")
@login_required
def create_session(course_id):
    owned_course(course_id)
    data = json_body(SESSION_SCHEMA)
    session = ChatSession(
        user_id=g.user.id,
        course_id=course_id,
        title=data.get("title") or "New session",
        document_filter=validate_document_ids(course_id, data.get("document_ids")),
    )
    db.session.add(session)
    db.session.commit()
    return jsonify({"session": session.to_dict()}), 201


@bp.get("/sessions/<int:session_id>")
@login_required
def get_session(session_id):
    return jsonify({"session": owned_session(session_id).to_dict(with_messages=True)})


@bp.patch("/sessions/<int:session_id>")
@login_required
def update_session(session_id):
    session = owned_session(session_id)
    data = json_body(SESSION_SCHEMA, partial=True)
    if "title" in data:
        session.title = data["title"] or session.title
    if "document_ids" in data:
        session.document_filter = validate_document_ids(session.course_id, data["document_ids"])
    db.session.commit()
    return jsonify({"session": session.to_dict()})


@bp.delete("/sessions/<int:session_id>")
@login_required
def delete_session(session_id):
    db.session.delete(owned_session(session_id))
    db.session.commit()
    return "", 204


@bp.post("/sessions/<int:session_id>/messages")
@login_required
@rate_limit("llm")
def ask(session_id):
    session = owned_session(session_id)
    data = json_body(QUESTION_SCHEMA)
    _require_ready(session.course_id)
    message = rag.answer(g.user, session, data["question"])
    return jsonify({"message": message.to_dict(), "session_id": session.id})


@bp.post("/sessions/<int:session_id>/stream")
@login_required
@rate_limit("llm")
def ask_stream(session_id):
    session = owned_session(session_id)
    data = json_body(QUESTION_SCHEMA)
    _require_ready(session.course_id)
    generator = rag.answer_stream(g.user.id, session.id, data["question"])
    return Response(
        stream_with_context(generator),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@bp.post("/courses/<int:course_id>/ask")
@login_required
@rate_limit("llm")
def quick_ask(course_id):
    """One shot question without managing sessions; reuses a per course scratch session."""
    owned_course(course_id)
    data = json_body(QUESTION_SCHEMA)
    _require_ready(course_id)
    session = ChatSession.query.filter_by(user_id=g.user.id, course_id=course_id, title="Quick questions").first()
    if session is None:
        session = ChatSession(user_id=g.user.id, course_id=course_id, title="Quick questions")
        db.session.add(session)
        db.session.commit()
    message = rag.answer(g.user, session, data["question"])
    return jsonify({"message": message.to_dict(), "session_id": session.id})


@bp.post("/messages/<int:message_id>/feedback")
@login_required
def feedback(message_id):
    message = db.session.get(Message, message_id)
    if message is None or message.session.user_id != g.user.id or message.role != "assistant":
        raise NotFound("Message not found")
    data = json_body({"value": Field(int, required=True, choices=(-1, 1))})
    message.feedback = data["value"]
    db.session.commit()
    return jsonify({"message": message.to_dict()})


@bp.get("/sessions/<int:session_id>/export")
@login_required
def export_session(session_id):
    session = owned_session(session_id)
    lines = [f"# {session.title}", ""]
    for msg in session.messages:
        speaker = "You" if msg.role == "user" else "StudyMate"
        lines.append(f"**{speaker}:** {msg.content}")
        for cite in msg.citations or []:
            page = f", page {cite['page']}" if cite.get("page") else ""
            lines.append(f"  - [{cite.get('marker')}] {cite.get('filename')}{page}")
        lines.append("")
    return Response(
        "\n".join(lines),
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="session-{session.id}.md"'},
    )


@bp.get("/sessions/<int:session_id>/messages")
@login_required
def list_messages(session_id):
    session = owned_session(session_id)
    query = Message.query.filter_by(session_id=session.id).order_by(Message.id)
    items, meta = paginate(query, default_per_page=50)
    return jsonify({"items": [m.to_dict() for m in items], "meta": meta})
