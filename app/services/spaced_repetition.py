"""SM-2 spaced repetition, the algorithm behind SuperMemo and early Anki.

Grades follow the original 0 to 5 scale. The UI only exposes four buttons,
mapped as: again=1, hard=3, good=4, easy=5.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

MIN_EASE = 1.3
BUTTONS = {"again": 1, "hard": 3, "good": 4, "easy": 5}


@dataclass
class ReviewResult:
    ease: float
    interval_days: int
    repetitions: int
    lapses: int
    due_at: datetime


def schedule(ease, interval_days, repetitions, lapses, grade, now):
    if not 0 <= grade <= 5:
        raise ValueError("grade must be between 0 and 5")

    if grade < 3:
        repetitions = 0
        interval_days = 1
        lapses += 1
    else:
        if repetitions == 0:
            interval_days = 1
        elif repetitions == 1:
            interval_days = 6
        else:
            interval_days = max(1, round(interval_days * ease))
        if grade == 5 and repetitions >= 1:
            interval_days = round(interval_days * 1.15)  # small easy bonus
        repetitions += 1

    ease = ease + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02))
    ease = max(MIN_EASE, round(ease, 3))
    interval_days = min(interval_days, 365)
    due = now + (timedelta(minutes=10) if grade < 3 else timedelta(days=interval_days))
    return ReviewResult(ease, interval_days, repetitions, lapses, due)


def apply_review(card, grade, now):
    result = schedule(card.ease, card.interval_days, card.repetitions, card.lapses, grade, now)
    card.ease = result.ease
    card.interval_days = result.interval_days
    card.repetitions = result.repetitions
    card.lapses = result.lapses
    card.due_at = result.due_at
    card.last_reviewed_at = now
    return card


def preview(card, now):
    """What each button would do, so the UI can label them (like '10m', '4d')."""
    out = {}
    for name, grade in BUTTONS.items():
        r = schedule(card.ease, card.interval_days, card.repetitions, card.lapses, grade, now)
        delta = r.due_at - now
        if delta < timedelta(hours=1):
            label = f"{int(delta.total_seconds() // 60)}m"
        else:
            label = f"{delta.days}d"
        out[name] = label
    return out
