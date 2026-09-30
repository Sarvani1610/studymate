from datetime import date, datetime, timedelta

from app.services.spaced_repetition import schedule


def test_generate_and_review_flashcards(client, auth, course, ready_doc):
    resp = client.post(f"/api/courses/{course['id']}/flashcards/generate", json={"count": 6}, headers=auth)
    assert resp.status_code == 201, resp.get_json()
    cards = resp.get_json()["created"]
    assert 1 <= len(cards) <= 6
    assert all(c["front"] and c["back"] for c in cards)

    again = client.post(f"/api/courses/{course['id']}/flashcards/generate", json={"count": 6}, headers=auth)
    assert again.get_json()["skipped_duplicates"] >= 1

    queue = client.get(f"/api/flashcards/review?course_id={course['id']}", headers=auth).get_json()
    assert queue["total_due"] >= len(cards)
    first = queue["cards"][0]
    assert set(first["buttons"]) == {"again", "hard", "good", "easy"}

    reviewed = client.post(f"/api/flashcards/{first['id']}/review", json={"rating": "good"}, headers=auth)
    card = reviewed.get_json()["flashcard"]
    assert card["repetitions"] == 1 and card["interval_days"] == 1

    bad = client.post(f"/api/flashcards/{first['id']}/review", json={}, headers=auth)
    assert bad.status_code == 422


def test_manual_card_crud(client, auth, course):
    resp = client.post(f"/api/courses/{course['id']}/flashcards",
                       json={"front": "Big O of heap insert?", "back": "O(log n)"}, headers=auth)
    card = resp.get_json()["flashcard"]
    resp = client.patch(f"/api/flashcards/{card['id']}", json={"suspended": True}, headers=auth)
    assert resp.get_json()["flashcard"]["suspended"] is True
    due = client.get(f"/api/courses/{course['id']}/flashcards?due=true", headers=auth).get_json()
    assert due["meta"]["total"] == 0
    assert client.delete(f"/api/flashcards/{card['id']}", headers=auth).status_code == 204


def test_sm2_intervals_grow_and_reset():
    now = datetime(2026, 9, 1, 12, 0)
    r1 = schedule(2.5, 0, 0, 0, 4, now)
    r2 = schedule(r1.ease, r1.interval_days, r1.repetitions, r1.lapses, 4, now)
    r3 = schedule(r2.ease, r2.interval_days, r2.repetitions, r2.lapses, 4, now)
    assert (r1.interval_days, r2.interval_days) == (1, 6)
    assert r3.interval_days == round(6 * r2.ease)
    lapse = schedule(r3.ease, r3.interval_days, r3.repetitions, r3.lapses, 1, now)
    assert lapse.repetitions == 0 and lapse.lapses == 1
    assert lapse.due_at - now == timedelta(minutes=10)
    assert lapse.ease >= 1.3


def test_quiz_flow_updates_mastery(client, auth, course, ready_doc):
    resp = client.post(f"/api/courses/{course['id']}/quizzes", json={"count": 4, "difficulty": "medium"},
                       headers=auth)
    assert resp.status_code == 201, resp.get_json()
    quiz = resp.get_json()["quiz"]
    assert quiz["question_count"] >= 2
    q0 = quiz["questions"][0]
    assert "answer_index" not in q0 and len(q0["options"]) == 4

    answers = [{"question_id": q["id"], "choice": 0} for q in quiz["questions"]]
    attempt = client.post(f"/api/quizzes/{quiz['id']}/attempts", json={"answers": answers, "duration_s": 90},
                          headers=auth).get_json()["attempt"]
    assert attempt["total"] == quiz["question_count"]
    assert len(attempt["breakdown"]) == attempt["total"]

    revealed = client.get(f"/api/quizzes/{quiz['id']}", headers=auth).get_json()["quiz"]
    assert "answer_index" in revealed["questions"][0]

    mastery = client.get(f"/api/courses/{course['id']}/mastery", headers=auth).get_json()
    assert len(mastery["items"]) >= 1

    bad = client.post(f"/api/quizzes/{quiz['id']}/attempts", json={"answers": [{"question_id": 99, "choice": 0}]},
                      headers=auth)
    assert bad.status_code == 422


def test_adaptive_quiz_targets_weak_topics(client, auth, course, ready_doc):
    quiz = client.post(f"/api/courses/{course['id']}/quizzes", json={"count": 4}, headers=auth).get_json()["quiz"]
    full = client.get(f"/api/quizzes/{quiz['id']}", headers=auth)  # answers hidden before attempt
    wrong = []
    from app.extensions import db
    from app.models import Quiz

    stored = db.session.get(Quiz, quiz["id"])
    for q in stored.questions:
        wrong.append({"question_id": q["id"], "choice": (q["answer_index"] + 1) % 4})
    for _ in range(2):
        client.post(f"/api/quizzes/{quiz['id']}/attempts", json={"answers": wrong}, headers=auth)
    assert full.status_code == 200

    weak = client.get(f"/api/courses/{course['id']}/mastery", headers=auth).get_json()["weak_topics"]
    assert weak
    adaptive = client.post(f"/api/courses/{course['id']}/quizzes", json={"count": 3, "adaptive": True},
                           headers=auth).get_json()
    assert set(weak) <= set(adaptive["focus_topics"])


def test_study_guide_with_plan(client, auth, course, ready_doc):
    exam = (date.today() + timedelta(days=6)).isoformat()
    resp = client.post(f"/api/courses/{course['id']}/study-guides",
                       json={"exam_date": exam, "hours_per_day": 1.5, "focus_topics": ["heapsort"]}, headers=auth)
    assert resp.status_code == 201, resp.get_json()
    guide = resp.get_json()["study_guide"]
    assert "## Key concepts" in guide["content"]
    assert "## Study plan" in guide["content"]
    assert len(guide["plan"]) == 6
    assert guide["plan"][-1]["tasks"][0].startswith("Full practice quiz")
    assert "heapsort" in guide["focus_topics"]
    md = client.get(f"/api/study-guides/{guide['id']}/markdown", headers=auth)
    assert md.mimetype == "text/markdown"


def test_stats_and_usage(client, auth, course, ready_doc):
    session = client.post(f"/api/courses/{course['id']}/sessions", json={}, headers=auth).get_json()["session"]
    client.post(f"/api/sessions/{session['id']}/messages", json={"question": "What is a heap?"}, headers=auth)
    stats = client.get("/api/me/stats", headers=auth).get_json()
    assert stats["questions_last_7_days"] == 1
    assert stats["streak_days"] == 1
    assert stats["documents"]["ready"] == 1
    usage = client.get("/api/me/usage", headers=auth).get_json()
    assert usage["quota"]["used"] > 0
