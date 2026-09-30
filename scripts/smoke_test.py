"""End to end smoke test against whatever DATABASE_URL and REDIS_URL point at.

CI runs this against real Postgres (pgvector + full text search) after seeding,
because the pytest suite only covers the SQLite code path.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app import create_app  # noqa: E402


def check(label, resp, expected=200):
    ok = resp.status_code == expected
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {resp.status_code}")
    if not ok:
        print(resp.get_data(as_text=True)[:500])
        sys.exit(1)
    return resp.get_json()


def main():
    app = create_app(os.getenv("APP_ENV", "development"))
    client = app.test_client()

    check("ready", client.get("/health/ready"))
    login = check("login", client.post("/api/auth/login",
                                       json={"email": "demo@studymate.local", "password": "demo-pass-2026"}))
    headers = {"Authorization": f"Bearer {login['access_token']}"}
    course_id = check("courses", client.get("/api/courses", headers=headers))["items"][0]["id"]

    results = check("search", client.get(f"/api/courses/{course_id}/search?q=priority queue", headers=headers))
    assert results["results"], "search returned nothing"
    print("     top hit:", results["results"][0]["section"], results["results"][0]["matched_by"])

    session = check("session", client.post(f"/api/courses/{course_id}/sessions", json={}, headers=headers), 201)
    answer = check("ask", client.post(f"/api/sessions/{session['session']['id']}/messages",
                                      json={"question": "Why does quicksort degrade to quadratic time?"},
                                      headers=headers))
    assert answer["message"]["citations"], "answer had no citations"
    print("     answer:", answer["message"]["content"][:160])

    check("quiz", client.post(f"/api/courses/{course_id}/quizzes", json={"count": 3}, headers=headers), 201)
    check("cards", client.post(f"/api/courses/{course_id}/flashcards/generate", json={"count": 5},
                               headers=headers), 201)
    check("guide", client.post(f"/api/courses/{course_id}/study-guides", json={}, headers=headers), 201)
    check("stats", client.get("/api/me/stats", headers=headers))
    print("smoke test passed")


if __name__ == "__main__":
    main()
