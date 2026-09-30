"""Load test: simulated students using the platform the way real ones do.

Weights come from what I expect a normal study session to look like: lots of
questions and flashcard reviews, some searching, occasional quizzes and
summaries, rare uploads. Accounts are pre-created with
`python scripts/seed_demo.py --users 500` so login is not the bottleneck.

    locust -f loadtest/locustfile.py --headless -u 500 -r 25 -t 10m --host http://localhost:8080
"""
import itertools
import random
import threading

from locust import HttpUser, between, events, task

PASSWORD = "demo-pass-2026"
QUESTIONS = [
    "What is a binary search tree?",
    "How does a heap keep its shape?",
    "What is the difference between BFS and DFS?",
    "Why does quicksort degrade to quadratic time?",
    "Explain the load factor of a hash table",
    "When should I use a red black tree?",
    "What is amortized analysis?",
    "How does Dijkstra's algorithm use a priority queue?",
    "What is a stable sort?",
    "What does it mean for a tree to be balanced?",
]
SEARCHES = ["heap", "hash table", "rotation", "graph traversal", "big o", "merge sort", "priority queue"]

_counter = itertools.count()
_lock = threading.Lock()


def next_account():
    with _lock:
        n = next(_counter)
    return f"loadtest{n % 500:04d}@studymate.local"


class Student(HttpUser):
    wait_time = between(2, 8)

    def on_start(self):
        self.email = next_account()
        self.headers, self.course_id, self.session_id = {}, None, None
        resp = self.client.post("/api/auth/login", json={"email": self.email, "password": PASSWORD},
                                name="/api/auth/login")
        if resp.status_code != 200:
            return  # tasks below all check for a course/session and skip quietly
        self.headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        courses = self.client.get("/api/courses", headers=self.headers, name="/api/courses").json()
        self.course_id = courses["items"][0]["id"] if courses.get("items") else None
        if self.course_id:
            s = self.client.post(f"/api/courses/{self.course_id}/sessions", json={}, headers=self.headers,
                                 name="/api/courses/[id]/sessions")
            self.session_id = s.json()["session"]["id"] if s.status_code == 201 else None

    @task(10)
    def ask_question(self):
        if not self.session_id:
            return
        self.client.post(f"/api/sessions/{self.session_id}/messages",
                         json={"question": random.choice(QUESTIONS)}, headers=self.headers,
                         name="/api/sessions/[id]/messages")

    @task(3)
    def ask_streaming(self):
        if not self.session_id:
            return
        with self.client.post(f"/api/sessions/{self.session_id}/stream",
                              json={"question": random.choice(QUESTIONS)}, headers=self.headers,
                              name="/api/sessions/[id]/stream", stream=True, catch_response=True) as resp:
            body = resp.content.decode("utf-8", "ignore")
            if "event: done" not in body:
                resp.failure("stream ended without a done event")

    @task(6)
    def review_cards(self):
        if not self.headers:
            return
        queue = self.client.get("/api/flashcards/review?limit=5", headers=self.headers,
                                name="/api/flashcards/review").json()
        for card in queue.get("cards", [])[:3]:
            self.client.post(f"/api/flashcards/{card['id']}/review",
                             json={"rating": random.choice(["again", "hard", "good", "good", "easy"])},
                             headers=self.headers, name="/api/flashcards/[id]/review")

    @task(4)
    def search(self):
        if self.course_id:
            self.client.get(f"/api/courses/{self.course_id}/search?q={random.choice(SEARCHES)}",
                            headers=self.headers, name="/api/courses/[id]/search")

    @task(2)
    def dashboard(self):
        if self.headers:
                self.client.get("/api/me/stats", headers=self.headers, name="/api/me/stats")

    @task(1)
    def quiz(self):
        if not self.course_id:
            return
        resp = self.client.post(f"/api/courses/{self.course_id}/quizzes", json={"count": 5},
                                headers=self.headers, name="/api/courses/[id]/quizzes")
        if resp.status_code != 201:
            return
        quiz = resp.json()["quiz"]
        answers = [{"question_id": q["id"], "choice": random.randint(0, 3)} for q in quiz["questions"]]
        self.client.post(f"/api/quizzes/{quiz['id']}/attempts", json={"answers": answers},
                         headers=self.headers, name="/api/quizzes/[id]/attempts")

    @task(1)
    def flashcards_generate(self):
        if self.course_id:
            self.client.post(f"/api/courses/{self.course_id}/flashcards/generate", json={"count": 5},
                             headers=self.headers, name="/api/courses/[id]/flashcards/generate")


@events.quitting.add_listener
def _check_thresholds(environment, **kwargs):
    """Fail the run (non zero exit) if error rate or p95 latency goes over budget."""
    stats = environment.stats.total
    if stats.num_requests == 0:
        return
    error_rate = stats.num_failures / stats.num_requests
    p95 = stats.get_response_time_percentile(0.95)
    print(f"requests={stats.num_requests} error_rate={error_rate:.4f} p95_ms={p95}")
    if error_rate > 0.01 or p95 > 2500:
        environment.process_exit_code = 1
