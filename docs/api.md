# API reference

All endpoints are JSON unless noted. Authenticated endpoints need `Authorization: Bearer <access_token>`. List endpoints accept `page` and `per_page` (max 100) and return `{"items": [...], "meta": {"page", "per_page", "total", "pages", "has_next", "has_prev"}}`.

Errors always look like this:

```json
{"error": {"code": "validation_error", "message": "Request body failed validation",
           "details": {"password": "must be at least 8 characters"}, "request_id": "4f1c2a9b0d3e7a11"}}
```

Rate limited responses are `429` with `Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset` headers.

## Auth

| Method | Path | Notes |
|---|---|---|
| POST | `/api/auth/register` | `email`, `password` (8+ chars, a letter and a number), `display_name`, optional `study_level` (intro, intermediate, advanced). Returns the user and a token pair. |
| POST | `/api/auth/login` | `email`, `password` |
| POST | `/api/auth/refresh` | `refresh_token`. The old refresh token stops working. |
| POST | `/api/auth/logout` | Revokes the access token, and the refresh token if sent. |
| GET | `/api/auth/me` | Current user |
| PATCH | `/api/auth/me` | `display_name`, `study_level` |
| POST | `/api/auth/change-password` | `current_password`, `new_password`. Returns a new token pair. |

```bash
curl -X POST localhost:8080/api/auth/login -H 'content-type: application/json' \
  -d '{"email": "demo@studymate.local", "password": "demo-pass-2026"}'
```

## Courses

| Method | Path | Notes |
|---|---|---|
| GET | `/api/courses` | Includes document counts |
| POST | `/api/courses` | `name`, `code` (stored uppercase, unique per student), optional `description`, `exam_date` |
| GET | `/api/courses/{id}` | |
| PATCH | `/api/courses/{id}` | Any of the create fields |
| DELETE | `/api/courses/{id}` | Deletes documents, stored files, chats, cards and quizzes |
| GET | `/api/courses/{id}/topics` | Key concepts extracted from the material. `limit` 5 to 60. |
| GET | `/api/courses/{id}/search?q=...&k=8&document_ids=1,2` | Hybrid search. Each result has `score`, `vector_score`, `keyword_score` and `matched_by`. |

## Documents

| Method | Path | Notes |
|---|---|---|
| POST | `/api/courses/{id}/documents` | Multipart with `file` and optional `title`. Returns `202` with the document. Re-uploading the same file returns `200` with `"duplicate": true`. |
| GET | `/api/courses/{id}/documents?status=ready` | |
| GET | `/api/documents/{id}` | Includes the list of section headings found |
| GET | `/api/documents/{id}/chunks` | The chunks the document was split into |
| GET | `/api/documents/{id}/download` | Redirects to a presigned S3 URL, or streams the file locally |
| POST | `/api/documents/{id}/reprocess` | Runs ingestion again and bumps the version |
| DELETE | `/api/documents/{id}` | |
| GET | `/api/documents/{id}/summary?style=brief` | `brief`, `detailed` or `bullets`. Stored per document version, `"cached": true` on repeat calls. |
| POST | `/api/documents/{id}/summary?style=brief` | Regenerate |

```bash
curl -X POST localhost:8080/api/courses/1/documents -H "Authorization: Bearer $TOKEN" \
  -F file=@lecture06_sorting.pdf -F title="Sorting"
```

## Chat

| Method | Path | Notes |
|---|---|---|
| POST | `/api/courses/{id}/sessions` | Optional `title`, `document_ids` to restrict answers to certain files |
| GET | `/api/courses/{id}/sessions` | |
| GET | `/api/sessions/{id}` | With all messages |
| PATCH | `/api/sessions/{id}` | `title`, `document_ids` |
| DELETE | `/api/sessions/{id}` | |
| POST | `/api/sessions/{id}/messages` | `question`. Returns the assistant message. |
| POST | `/api/sessions/{id}/stream` | `question`. Server sent events, see below. |
| GET | `/api/sessions/{id}/messages` | Paginated |
| GET | `/api/sessions/{id}/export` | Markdown download |
| POST | `/api/courses/{id}/ask` | One off question without managing a session |
| POST | `/api/messages/{id}/feedback` | `value`: 1 or -1 |

An assistant message:

```json
{
  "id": 42, "role": "assistant",
  "content": "Quicksort degrades to quadratic time when the pivot is repeatedly the smallest or largest element [1].",
  "citations": [{"marker": 1, "document_id": 2, "filename": "lecture06_sorting.md", "page": null,
                 "section": "Quicksort", "snippet": "Quicksort picks a pivot, partitions..."}],
  "cached": false, "grounded": true, "latency_ms": 840
}
```

Stream events, in order:

```
event: meta    data: {"cached": false, "grounded": true, "sources": [...], "search_query": "..."}
event: token   data: {"t": "Quicksort "}
event: token   data: {"t": "degrades "}
...
event: done    data: {"message": { ...the saved assistant message... }}
```

If something fails after the stream started, an `error` event with `message` and `code` is sent instead of `done`.

## Flashcards

| Method | Path | Notes |
|---|---|---|
| POST | `/api/courses/{id}/flashcards/generate` | `count` (1 to 40), optional `document_ids`, `topics`. Skips cards whose front already exists. |
| GET | `/api/courses/{id}/flashcards?due=true&topic=...` | |
| POST | `/api/courses/{id}/flashcards` | Manual card: `front`, `back`, optional `topic` |
| PATCH | `/api/flashcards/{id}` | `front`, `back`, `topic`, `suspended` |
| DELETE | `/api/flashcards/{id}` | |
| GET | `/api/flashcards/review?course_id=1&limit=20` | Due cards with a preview of each button, like `{"again": "10m", "good": "6d"}` |
| POST | `/api/flashcards/{id}/review` | `rating` (again, hard, good, easy) or `grade` (0 to 5) |

## Quizzes

| Method | Path | Notes |
|---|---|---|
| POST | `/api/courses/{id}/quizzes` | `count` (1 to 25), `difficulty`, optional `document_ids`, `focus_topics`, `adaptive`, `title`. Answers are hidden until the first attempt. |
| GET | `/api/courses/{id}/quizzes` | With attempt count and best score |
| GET | `/api/quizzes/{id}` | |
| DELETE | `/api/quizzes/{id}` | |
| POST | `/api/quizzes/{id}/attempts` | `answers`: `[{"question_id": 0, "choice": 2}, ...]` (`choice` can be null to skip), optional `duration_s` |
| GET | `/api/quizzes/{id}/attempts` | |
| GET | `/api/courses/{id}/mastery` | Accuracy per topic and the current weak topics |

## Study guides

| Method | Path | Notes |
|---|---|---|
| POST | `/api/courses/{id}/study-guides` | Optional `exam_date` (falls back to the course's), `hours_per_day`, `focus_topics`, `document_ids` |
| GET | `/api/courses/{id}/study-guides` | |
| GET | `/api/study-guides/{id}` | Markdown `content` plus a structured `plan` |
| GET | `/api/study-guides/{id}/markdown` | Download |
| DELETE | `/api/study-guides/{id}` | |

## Progress

| Method | Path | Notes |
|---|---|---|
| GET | `/api/me/stats?course_id=1` | Streak, activity by day, cards due, quiz trend, weakest and strongest topics, answer feedback, token quota |
| GET | `/api/me/usage` | Token quota and usage by feature |

## Admin

Needs a user with the `admin` role (`flask --app wsgi create-admin --email ... --password ...`).

| Method | Path | Notes |
|---|---|---|
| GET | `/api/admin/stats` | Last 24 hours: active users, usage and latency by feature, answer cache hit rate, documents by status, Celery workers |
| GET | `/api/admin/users?q=...` | |
| PATCH | `/api/admin/users/{id}` | `is_active`, `role` |
| POST | `/api/admin/cache/purge` | `namespace`: answer, search, qemb, topics or stats |
| POST | `/api/admin/courses/{id}/reindex` | Reprocess every document in a course |

## Operations

| Path | Notes |
|---|---|
| `/health/live` | Process is up |
| `/health/ready` | Postgres and Redis reachable, with timings. `503` if not. |
| `/metrics` | Prometheus. Blocked outside the private network by nginx. |
| `/api/version` | Version, environment and active providers |
