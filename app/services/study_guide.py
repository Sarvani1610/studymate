"""Personalized study guides.

Personalization comes from three places:
  - quiz history: topics with low accuracy become "weak topics" and get extra
    explanation plus a second review slot in the plan,
  - the student's level (intro, intermediate, advanced) which changes tone,
  - the exam date and hours per day, which shape the day by day plan.
"""
import logging
from datetime import date, timedelta

from ..extensions import db
from ..models import StudyGuide, TopicMastery, UsageEvent
from ..utils.text import split_sentences, truncate
from . import quota
from .generators import _material, load_chunks
from .keywords import definitions, extract_keyphrases
from .llm import get_llm
from .prompts import GUIDE_SYSTEM, GUIDE_USER, LEVEL_HINTS
from .retrieval import retrieve
from .summarizer import extractive_summary

log = logging.getLogger(__name__)
WEAK_THRESHOLD = 0.7


def weak_topics(user_id, course_id, limit=8):
    rows = (
        TopicMastery.query.filter_by(user_id=user_id, course_id=course_id)
        .filter(TopicMastery.attempts >= 2)
        .all()
    )
    weak = [r for r in rows if r.accuracy < WEAK_THRESHOLD]
    weak.sort(key=lambda r: (r.accuracy, -r.attempts))
    return [r.topic for r in weak[:limit]]


def build_plan(topics, weak, exam_date, today, hours_per_day):
    if exam_date and exam_date > today:
        days = min((exam_date - today).days, 30)
    else:
        days = 7
    days = max(days, 2)
    minutes = int(hours_per_day * 60)
    study_days = days - 1  # the last day is always a full review

    queue = list(dict.fromkeys(weak + topics))
    if not queue:
        queue = ["review all lecture notes"]
    slots = [[] for _ in range(study_days)]
    for i, topic in enumerate(queue):
        slots[i % study_days].append(topic)
    # Weak topics come back a few days later for a second pass (spacing effect).
    for i, topic in enumerate(weak):
        revisit = min(study_days - 1, (i % study_days) + 3)
        if topic not in slots[revisit]:
            slots[revisit].append(topic)

    plan = []
    for offset, topics_today in enumerate(slots):
        day = today + timedelta(days=offset)
        tasks = [f"Read and summarize: {t}" for t in topics_today[:3]]
        if any(t in weak for t in topics_today):
            tasks.append("Take a short quiz on today's weak topics")
        tasks.append("Review due flashcards")
        plan.append({"date": day.isoformat(), "focus": topics_today[:4], "tasks": tasks, "minutes": minutes})
    final_day = today + timedelta(days=study_days)
    plan.append({
        "date": final_day.isoformat(),
        "focus": weak[:4] or queue[:4],
        "tasks": ["Full practice quiz across the course", "Re-read summaries of weak topics", "Sleep early"],
        "minutes": minutes,
    })
    return plan


def _plan_markdown(plan):
    lines = ["## Study plan", "", "| Day | Focus | Tasks | Time |", "|---|---|---|---|"]
    for item in plan:
        lines.append(
            f"| {item['date']} | {', '.join(item['focus'])} | {'; '.join(item['tasks'])} | {item['minutes']} min |"
        )
    return "\n".join(lines)


def _offline_guide(course, chunks, concepts, weak, defs):
    texts = [c.text for c in chunks]
    parts = [f"# Study guide: {course.name}", "", "## Overview", "", extractive_summary(texts, 5, "brief"), ""]

    parts += ["## Key concepts", ""]
    for term in concepts[:12]:
        explanation = defs.get(term)
        if not explanation:
            hits, _ = retrieve(course.id, term, k=1, use_mmr=False)
            if hits:
                sentences = [s for s in split_sentences(hits[0].chunk.text) if term in s.lower()]
                explanation = sentences[0] if sentences else truncate(hits[0].chunk.text, 220)
        if explanation:
            parts.append(f"- **{term}**: {truncate(explanation, 300)}")
    parts.append("")

    parts += ["## Where to focus", ""]
    if weak:
        for topic in weak:
            hits, _ = retrieve(course.id, topic, k=2, use_mmr=False)
            parts.append(f"### {topic}")
            for hit in hits:
                where = hit.chunk.document.filename if hit.chunk.document else ""
                if hit.chunk.page:
                    where += f", page {hit.chunk.page}"
                parts.append(f"{truncate(hit.chunk.text, 400)} ({where})")
            parts.append("")
    else:
        parts += ["No weak topics yet. Take a quiz or two and the next guide will adapt to your results.", ""]

    parts += ["## Practice questions", ""]
    picks = (weak + concepts)[:5]
    templates = [
        "Explain {a} in your own words and give an example.",
        "How does {a} relate to {b}?",
        "What problem does {a} solve, and when would you not use it?",
        "Compare {a} and {b}. When is each one the better choice?",
        "Walk through a small example that uses {a}.",
    ]
    for i, topic in enumerate(picks):
        other = picks[(i + 1) % len(picks)] if len(picks) > 1 else "the rest of the course"
        parts.append(f"{i + 1}. {templates[i % len(templates)].format(a=topic, b=other)}")
    return "\n".join(parts)


def generate_guide(user, course, exam_date=None, hours_per_day=2.0, focus_topics=None, document_ids=None,
                   today=None):
    today = today or date.today()
    exam_date = exam_date or course.exam_date
    weak = list(dict.fromkeys((focus_topics or []) + weak_topics(user.id, course.id)))
    chunks = load_chunks(course.id, document_ids=document_ids, limit=80)
    texts = [c.text for c in chunks]
    concepts = extract_keyphrases(texts, top_n=15)
    defs = definitions(texts, concepts)
    plan = build_plan(concepts[:10], weak, exam_date, today, hours_per_day)

    llm = get_llm()
    tokens_in = tokens_out = 0
    if llm.generative:
        material = _material(chunks, budget=3500)
        quota.ensure_budget(user, len(material) // 4)
        result = llm.complete(
            GUIDE_SYSTEM.format(level_hint=LEVEL_HINTS.get(user.study_level, "")),
            GUIDE_USER.format(
                course=course.name,
                exam_date=exam_date.isoformat() if exam_date else "not set",
                weak_topics=", ".join(weak) or "none yet",
                concepts=", ".join(concepts),
                text=material,
            ),
            task="study_guide",
            max_tokens=1600,
        )
        body = f"# Study guide: {course.name}\n\n{result.text.strip()}"
        tokens_in, tokens_out, model = result.tokens_in, result.tokens_out, result.model
    else:
        body = _offline_guide(course, chunks, concepts, weak, defs)
        model = "extractive"

    content = body + "\n\n" + _plan_markdown(plan) + "\n"
    guide = StudyGuide(
        user_id=user.id,
        course_id=course.id,
        title=f"{course.code} guide for {exam_date.isoformat() if exam_date else today.isoformat()}",
        content=content,
        focus_topics=weak,
        plan=plan,
        exam_date=exam_date,
        hours_per_day=hours_per_day,
    )
    db.session.add(guide)
    db.session.add(UsageEvent(user_id=user.id, course_id=course.id, kind="study_guide",
                              tokens_in=tokens_in, tokens_out=tokens_out, model=model))
    db.session.commit()
    quota.record(user.id, tokens_in + tokens_out)
    return guide
