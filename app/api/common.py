from flask import g

from ..errors import NotFound
from ..extensions import db
from ..models import ChatSession, Course, Document, Flashcard, Quiz, StudyGuide


def owned_course(course_id):
    course = db.session.get(Course, course_id)
    if course is None or (course.owner_id != g.user.id and not g.user.is_admin):
        raise NotFound("Course not found")
    return course


def owned_document(document_id):
    doc = db.session.get(Document, document_id)
    if doc is None or (doc.owner_id != g.user.id and not g.user.is_admin):
        raise NotFound("Document not found")
    return doc


def owned(model, object_id, label):
    obj = db.session.get(model, object_id)
    if obj is None or (obj.user_id != g.user.id and not g.user.is_admin):
        raise NotFound(f"{label} not found")
    return obj


def owned_session(session_id):
    return owned(ChatSession, session_id, "Chat session")


def owned_quiz(quiz_id):
    return owned(Quiz, quiz_id, "Quiz")


def owned_card(card_id):
    return owned(Flashcard, card_id, "Flashcard")


def owned_guide(guide_id):
    return owned(StudyGuide, guide_id, "Study guide")


def validate_document_ids(course_id, document_ids):
    if not document_ids:
        return None
    found = {
        d.id for d in Document.query.filter(Document.course_id == course_id, Document.id.in_(document_ids)).all()
    }
    missing = sorted(set(document_ids) - found)
    if missing:
        raise NotFound("Some documents do not belong to this course", details={"missing": missing})
    return sorted(found)
