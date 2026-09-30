from flask import Blueprint, g, jsonify

from ..auth.decorators import login_required
from ..errors import ValidationError
from ..extensions import db
from ..models import Quiz, QuizAttempt, TopicMastery, UsageEvent, utcnow
from ..services.generators import generate_quiz, grade, load_chunks
from ..services.rate_limit import rate_limit
from ..services.study_guide import weak_topics
from ..utils.pagination import paginate
from ..utils.validation import Field, json_body
from .common import owned_course, owned_quiz, validate_document_ids

bp = Blueprint("quizzes", __name__, url_prefix="/api")

QUIZ_SCHEMA = {
    "count": Field(int, default=5, min_value=1, max_value=25),
    "difficulty": Field(str, default="medium", choices=("easy", "medium", "hard")),
    "document_ids": Field(list, item_kind=int, max_items=50),
    "focus_topics": Field(list, item_kind=str, max_items=10),
    "adaptive": Field(bool, default=False),
    "title": Field(str, max_len=200),
}
ATTEMPT_SCHEMA = {
    "answers": Field(list, required=True, max_items=100),
    "duration_s": Field(int, min_value=0, max_value=6 * 3600),
}


@bp.post("/courses/<int:course_id>/quizzes")
@login_required
@rate_limit("llm")
def create_quiz(course_id):
    course = owned_course(course_id)
    data = json_body(QUIZ_SCHEMA)
    focus = [t.strip().lower() for t in data.get("focus_topics") or [] if t.strip()]
    if data["adaptive"]:
        # Adaptive quizzes lean on whatever the student has been getting wrong.
        focus = list(dict.fromkeys(focus + weak_topics(g.user.id, course_id)))
    doc_ids = validate_document_ids(course_id, data.get("document_ids"))
    chunks = load_chunks(course_id, document_ids=doc_ids, topics=focus or None)
    questions, tokens = generate_quiz(g.user, chunks, count=data["count"], difficulty=data["difficulty"],
                                      focus_topics=focus)
    quiz = Quiz(
        user_id=g.user.id,
        course_id=course_id,
        title=data.get("title") or f"{course.code} {data['difficulty']} quiz",
        difficulty=data["difficulty"],
        questions=questions,
    )
    db.session.add(quiz)
    db.session.add(UsageEvent(user_id=g.user.id, course_id=course_id, kind="quiz", tokens_in=tokens))
    db.session.commit()
    return jsonify({"quiz": quiz.to_dict(), "focus_topics": focus}), 201


@bp.get("/courses/<int:course_id>/quizzes")
@login_required
def list_quizzes(course_id):
    owned_course(course_id)
    query = Quiz.query.filter_by(course_id=course_id, user_id=g.user.id).order_by(Quiz.created_at.desc())
    items, meta = paginate(query)
    out = []
    for quiz in items:
        best = max((a.score for a in quiz.attempts), default=None)
        item = quiz.to_dict()
        item.pop("questions")
        item.update({"attempts": len(quiz.attempts), "best_score": best})
        out.append(item)
    return jsonify({"items": out, "meta": meta})


@bp.get("/quizzes/<int:quiz_id>")
@login_required
def get_quiz(quiz_id):
    quiz = owned_quiz(quiz_id)
    return jsonify({"quiz": quiz.to_dict(reveal_answers=bool(quiz.attempts))})


@bp.delete("/quizzes/<int:quiz_id>")
@login_required
def delete_quiz(quiz_id):
    db.session.delete(owned_quiz(quiz_id))
    db.session.commit()
    return "", 204


def _update_mastery(user_id, course_id, breakdown):
    by_topic = {}
    for row in breakdown:
        stats = by_topic.setdefault(row["topic"][:120], [0, 0])
        stats[0] += 1 if row["correct"] else 0
        stats[1] += 1
    for topic, (correct, attempts) in by_topic.items():
        row = TopicMastery.query.filter_by(user_id=user_id, course_id=course_id, topic=topic).first()
        if row is None:
            row = TopicMastery(user_id=user_id, course_id=course_id, topic=topic, correct=0, attempts=0)
            db.session.add(row)
        row.correct += correct
        row.attempts += attempts
        row.updated_at = utcnow()


@bp.post("/quizzes/<int:quiz_id>/attempts")
@login_required
def submit_attempt(quiz_id):
    quiz = owned_quiz(quiz_id)
    data = json_body(ATTEMPT_SCHEMA)
    answers = []
    valid_ids = {q["id"] for q in quiz.questions}
    for item in data["answers"]:
        if not isinstance(item, dict) or not isinstance(item.get("question_id"), int):
            raise ValidationError("Each answer needs an integer question_id and choice")
        if item["question_id"] not in valid_ids:
            raise ValidationError(f"Unknown question_id {item['question_id']}")
        choice = item.get("choice")
        if choice is not None and (not isinstance(choice, int) or not 0 <= choice <= 3):
            raise ValidationError("choice must be an integer from 0 to 3, or null to skip")
        answers.append({"question_id": item["question_id"], "choice": choice})

    score, breakdown = grade(quiz, answers)
    attempt = QuizAttempt(quiz_id=quiz.id, user_id=g.user.id, answers=answers, score=score,
                          total=len(quiz.questions), duration_s=data.get("duration_s"), breakdown=breakdown)
    db.session.add(attempt)
    _update_mastery(g.user.id, quiz.course_id, breakdown)
    db.session.add(UsageEvent(user_id=g.user.id, course_id=quiz.course_id, kind="quiz_attempt"))
    db.session.commit()
    return jsonify({"attempt": attempt.to_dict()}), 201


@bp.get("/quizzes/<int:quiz_id>/attempts")
@login_required
def list_attempts(quiz_id):
    quiz = owned_quiz(quiz_id)
    query = QuizAttempt.query.filter_by(quiz_id=quiz.id).order_by(QuizAttempt.created_at.desc())
    items, meta = paginate(query)
    return jsonify({"items": [a.to_dict() for a in items], "meta": meta})


@bp.get("/courses/<int:course_id>/mastery")
@login_required
def mastery(course_id):
    owned_course(course_id)
    rows = TopicMastery.query.filter_by(user_id=g.user.id, course_id=course_id).all()
    items = sorted((r.to_dict() for r in rows), key=lambda r: r["accuracy"])
    return jsonify({"items": items, "weak_topics": weak_topics(g.user.id, course_id)})
