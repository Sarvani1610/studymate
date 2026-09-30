"""Per student progress numbers for the dashboard."""
from datetime import date, datetime, timedelta

from sqlalchemy import func

from ..extensions import db
from ..models import (
    ChatSession,
    Course,
    Document,
    Flashcard,
    Message,
    QuizAttempt,
    TopicMastery,
    UsageEvent,
    utcnow,
)
from . import cache, quota


def study_streak(days_active, today):
    """Consecutive days with activity ending today (or yesterday, so a streak
    does not look broken first thing in the morning)."""
    active = set(days_active)
    cursor = today if today in active else today - timedelta(days=1)
    streak = 0
    while cursor in active:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _to_date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def user_stats(user, course_id=None):
    key = cache.make_key("stats", user.id, course_id or "all", cache.course_version(course_id or 0))
    hit = cache.get_json(key, "stats")
    if hit:
        return hit

    now = utcnow()
    since = now - timedelta(days=30)

    courses_q = Course.query.filter_by(owner_id=user.id)
    if course_id:
        courses_q = courses_q.filter_by(id=course_id)
    course_ids = [c.id for c in courses_q.all()]

    docs = Document.query.filter(Document.course_id.in_(course_ids)).all() if course_ids else []
    events = UsageEvent.query.filter(UsageEvent.user_id == user.id, UsageEvent.created_at >= since)
    if course_id:
        events = events.filter(UsageEvent.course_id == course_id)
    events = events.all()

    questions_7d = sum(1 for e in events if e.kind == "chat" and e.created_at >= now - timedelta(days=7))
    active_days = {_to_date(e.created_at) for e in events}

    cards = Flashcard.query.filter(Flashcard.user_id == user.id, Flashcard.suspended.is_(False))
    if course_id:
        cards = cards.filter(Flashcard.course_id == course_id)
    total_cards = cards.count()
    due_cards = cards.filter(Flashcard.due_at <= now).count()
    mature_cards = cards.filter(Flashcard.interval_days >= 21).count()

    attempts = QuizAttempt.query.filter(QuizAttempt.user_id == user.id)
    if course_id:
        attempts = attempts.join(QuizAttempt.quiz).filter_by(course_id=course_id)
    attempts = attempts.order_by(QuizAttempt.created_at.desc()).limit(20).all()
    quiz_avg = (
        round(100.0 * sum(a.score for a in attempts) / max(1, sum(a.total for a in attempts)), 1)
        if attempts else None
    )

    mastery = TopicMastery.query.filter_by(user_id=user.id)
    if course_id:
        mastery = mastery.filter_by(course_id=course_id)
    mastery = sorted((m.to_dict() for m in mastery.all()), key=lambda m: m["accuracy"])

    feedback = (
        db.session.query(Message.feedback, func.count(Message.id))
        .join(ChatSession, ChatSession.id == Message.session_id)
        .filter(ChatSession.user_id == user.id, Message.feedback.isnot(None))
        .group_by(Message.feedback)
        .all()
    )

    by_day = {}
    for e in events:
        key_day = _to_date(e.created_at).isoformat()
        by_day[key_day] = by_day.get(key_day, 0) + 1

    stats = {
        "courses": len(course_ids),
        "documents": {
            "total": len(docs),
            "ready": sum(1 for d in docs if d.status == "ready"),
            "failed": sum(1 for d in docs if d.status == "failed"),
            "pages": sum(d.page_count or 0 for d in docs),
        },
        "questions_last_7_days": questions_7d,
        "active_days_last_30": len(active_days),
        "streak_days": study_streak(active_days, now.date()),
        "activity_by_day": dict(sorted(by_day.items())),
        "flashcards": {"total": total_cards, "due": due_cards, "mature": mature_cards},
        "quizzes": {
            "recent_attempts": len(attempts),
            "average_percent": quiz_avg,
            "trend": [a.to_dict()["percent"] for a in reversed(attempts)],
        },
        "weakest_topics": mastery[:5],
        "strongest_topics": list(reversed(mastery[-5:])) if mastery else [],
        "answer_feedback": {("helpful" if k == 1 else "not_helpful"): v for k, v in feedback},
        "token_quota": quota.summary(user),
    }
    cache.set_json(key, stats, 60)
    return stats


def platform_stats():
    now = utcnow()
    day_ago = now - timedelta(days=1)
    rows = (
        db.session.query(
            UsageEvent.kind,
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.tokens_in), 0),
            func.coalesce(func.sum(UsageEvent.tokens_out), 0),
            func.coalesce(func.avg(UsageEvent.latency_ms), 0),
        )
        .filter(UsageEvent.created_at >= day_ago)
        .group_by(UsageEvent.kind)
        .all()
    )
    cached = (
        db.session.query(func.count(UsageEvent.id))
        .filter(UsageEvent.created_at >= day_ago, UsageEvent.kind == "chat", UsageEvent.cached.is_(True))
        .scalar()
    )
    chats = sum(r[1] for r in rows if r[0] == "chat")
    status_counts = dict(db.session.query(Document.status, func.count(Document.id)).group_by(Document.status).all())
    active_users = (
        db.session.query(func.count(func.distinct(UsageEvent.user_id)))
        .filter(UsageEvent.created_at >= day_ago)
        .scalar()
    )
    return {
        "window": "last_24h",
        "active_users": active_users,
        "by_kind": [
            {"kind": k, "count": c, "tokens_in": int(ti), "tokens_out": int(to), "avg_latency_ms": round(float(lat), 1)}
            for k, c, ti, to, lat in rows
        ],
        "answer_cache_hit_rate": round(cached / chats, 3) if chats else None,
        "documents_by_status": status_counts,
    }
