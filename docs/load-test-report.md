# Load test report

**Goal:** check that the platform holds up with 500 students using it at the same time, and find whatever breaks first.

**Result:** after two fixes the test found, 500 concurrent simulated students ran for 8 minutes with 44,318 requests and zero failures. Throughput held at about 100 requests per second once all users were active. Median latency was 73 ms over the whole run and p95 was 990 ms, on a single 2 vCPU machine that was also running the database, the cache and the load generator.

## Setup

| | |
|---|---|
| Machine | 1 VM, 2 vCPU, 7 GB RAM. Postgres, Redis, the API and Locust all on the same box. |
| API | gunicorn, 3 gthread workers with 16 threads each, `DB_POOL_SIZE=10`, `DB_MAX_OVERFLOW=10` |
| Database | PostgreSQL 16 with pgvector, HNSW index on embeddings, GIN full text index |
| Cache | Redis 7 |
| Models | Offline providers (local embeddings, extractive answers). This measures the platform itself: auth, retrieval, database, caching, rate limiting. With a hosted model, answer latency would be dominated by the model's own response time. |
| Data | 501 student accounts, each with one course and three lecture documents (11 chunks per course, about 5,500 chunks total) |
| Tool | Locust 2, `loadtest/locustfile.py` |
| Shape | 500 users, spawned at 5 per second (fully ramped after 100 s), 8 minutes total, 2 to 8 seconds of think time between actions |

### What each simulated student does

| Action | Weight |
|---|---|
| Ask a question (JSON) | 10 |
| Review due flashcards (queue plus up to 3 reviews) | 6 |
| Search the course | 4 |
| Ask a question (streamed) | 3 |
| Open the progress dashboard | 2 |
| Generate and take a 5 question quiz | 1 |
| Generate flashcards | 1 |

Each student logs in once, loads their courses and opens a chat session on start. Questions come from a pool of 10 typical data structures questions.

All simulated students share one IP address, so for the test the `auth` rate limit was raised with `RATE_LIMIT_AUTH=5000/minute`. Every other limit stayed at its default, which is the point: the per user limits should not trip for normal use.

## Final run

| Endpoint | Requests | Failures | p50 (ms) | p95 (ms) | p99 (ms) | req/s |
|---|---:|---:|---:|---:|---:|---:|
| POST /api/sessions/[id]/messages | 15,281 | 0 | 110 | 1,100 | 2,100 | 31.9 |
| GET /api/flashcards/review | 9,122 | 0 | 34 | 620 | 1,600 | 19.0 |
| GET /api/courses/[id]/search | 6,122 | 0 | 51 | 880 | 1,800 | 12.8 |
| POST /api/sessions/[id]/stream | 4,540 | 0 | 81 | 980 | 2,000 | 9.5 |
| GET /api/me/stats | 3,043 | 0 | 52 | 860 | 1,700 | 6.3 |
| POST /api/courses/[id]/flashcards/generate | 1,581 | 0 | 77 | 910 | 1,700 | 3.3 |
| POST /api/courses/[id]/quizzes | 1,500 | 0 | 84 | 890 | 1,500 | 3.1 |
| POST /api/quizzes/[id]/attempts | 1,500 | 0 | 85 | 930 | 1,700 | 3.1 |
| POST /api/auth/login | 500 | 0 | 520 | 2,300 | 3,100 | 1.0 |
| GET /api/courses | 500 | 0 | 36 | 1,300 | 2,300 | 1.0 |
| POST /api/courses/[id]/sessions | 500 | 0 | 43 | 1,500 | 2,300 | 1.0 |
| POST /api/flashcards/[id]/review | 129 | 0 | 48 | 890 | 1,800 | 0.3 |
| **All** | **44,318** | **0** | **73** | **990** | **2,000** | **92.4** |

Once all 500 users were active (the last 6 minutes), Locust's rolling numbers sat at a median of about 101 requests per second (peak 108), a rolling p50 around 87 ms and a rolling p95 around 1.3 s.

Login is the slowest endpoint on purpose. Passwords are hashed with scrypt, which is designed to be expensive, and during the ramp up 5 logins per second were competing with everything else for 2 cores.

### Cache behaviour during the run

From the Prometheus counters after the run:

| Cache | Hits | Misses | Hit rate |
|---|---:|---:|---:|
| Query embeddings | 17,672 | 80 | 99.5% |
| Answers | 4,996 | 2,722 | 64.7% |
| Search results | 3,228 | 2,907 | 52.6% |

These are higher than real traffic would produce, because every simulated student draws from the same 10 questions. The answer cache is also per course, and each simulated student has their own course, so most of the answer misses are the first time each student asks each question. The embedding cache is shared across everyone, which is why it is close to 100 percent.

## What the test found

The final numbers came from the third run. The first two turned up real problems.

### 1. The global rate limit was keyed by IP, not by user

The first attempt ramped 25 users per second and most logins came back `429`. Part of that was the auth limit, which is expected with every user on one IP. But authenticated requests were also getting `429` from the global limit, which was supposed to be per user.

The cause: the global limit runs in a `before_request` hook, which fires before the `login_required` decorator loads the user. So the limiter never saw a user and fell back to the client IP for every request. In a real deployment, a whole campus behind a NAT, or a classroom on the same wifi, would have shared one bucket of 120 requests per minute.

Fix: the limiter now reads the user id from the bearer token itself (verifying the signature) when the user has not been loaded yet. The `/api/auth` routes are exempt from the global limit because they already have their own stricter scope.

### 2. Connection resets from worker recycling and keep-alive timing

With the limiter fixed and a slower ramp, 1,732 of 45,102 requests (3.8%) failed with `ConnectionResetError` or `RemoteDisconnected`. Nothing showed up in the application logs because the requests never reached the app.

Two things were closing connections that the client still thought were open:

- `max_requests = 2000` recycles a gunicorn worker every 2,000 requests. At about 94 requests per second over 3 workers, that is a recycle roughly every 64 seconds, and each recycle drops that worker's keep-alive connections. Raising it to 50,000 (with jitter) cut the failures to 82 (0.18%).
- The remaining resets matched the keep-alive race: gunicorn's default keep-alive is a few seconds, and with 2 to 8 seconds of think time the client often reused a connection right as the server closed it. Raising keep-alive to 75 seconds, longer than the 60 second ALB idle timeout, removed the rest. In production the same rule applies: the backend keep-alive has to outlast the load balancer's idle timeout.

| Run | Change | Requests | Failures | Failure rate |
|---|---|---:|---:|---:|
| 1 | first attempt, 25 users/s ramp | 1,040 | 681 | 65.5% (rate limit bug) |
| 2 | limiter fix, 5 users/s ramp | 45,102 | 1,732 | 3.84% |
| 3 | `max_requests` 2,000 to 50,000 | 45,208 | 82 | 0.18% |
| 4 | keep-alive 5 s to 75 s | 44,318 | 0 | 0.00% |

Run 1 was stopped early. The raw Locust output for run 3 and the final run (stats, per second history and the HTML report) is in `loadtest/results/`.

## Where the time goes

- The tail latency (p95 near 1 second) comes mostly from CPU contention. Locust simulating 500 users uses a large share of the two cores, and Postgres, Redis and three API workers share the rest. The p50 of 73 ms is a better picture of the request path itself.
- On the question path, retrieval (one vector query and one full text query) plus saving two messages and a usage event is the bulk of the work. Cache hits skip retrieval and the model call but still write the messages.
- `GET /api/courses` and `POST /sessions` look slow at p95 only because they run once per user at startup, right when logins are hashing passwords.

## Limits of this test

- The model is the offline extractive provider. A hosted model adds 1 to 5 seconds per answer and brings its own rate limits. The per user LLM rate limit and the daily token budget exist for that reason. The platform side has plenty of headroom; the model provider's quota would be the first real limit.
- Everything ran on one small machine, so this shows the design holds up rather than giving production capacity numbers. On AWS the database, cache and API run on separate hosts and the API scales out on CPU.
- 10 repeated questions make the answer cache look better than it would with real students.
- Uploads were not part of the steady state mix. Ingestion runs on the Celery worker, so it does not compete with API requests for threads, but it does compete for database writes.

## Reproducing it

```bash
python scripts/seed_demo.py --users 500
RATE_LIMIT_AUTH=5000/minute gunicorn -c gunicorn.conf.py wsgi:app
locust -f loadtest/locustfile.py --headless -u 500 -r 5 -t 8m --host http://localhost:8000 \
  --csv loadtest/results/run --html loadtest/results/report.html
```

The Locust script exits with a non zero code if the error rate goes over 1 percent or p95 goes over 2.5 seconds, so it can run as a CI gate.
