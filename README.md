# StudyMate

A study platform for university courses. Students upload their lecture notes, slides and readings, then ask questions about them, get summaries, generate flashcards and quizzes, and build a study plan for an upcoming exam. Every answer is grounded in the uploaded material and cites the file and page it came from.


## What it does

**Course material**
- Upload PDF, DOCX, PPTX, Markdown or plain text (up to 25 MB). Files are checked by extension and magic bytes, deduplicated by SHA-256, and stored on local disk or S3.
- Ingestion runs in a Celery worker: text extraction, header and footer removal, sentence aware chunking that never crosses a section heading, and embeddings stored in Postgres with pgvector.
- Documents can be reprocessed after changing chunk settings, and the admin can reindex a whole course.

**Asking questions**
- Hybrid retrieval: pgvector cosine similarity and Postgres full text search, merged with reciprocal rank fusion, then diversified with MMR so the context is not five copies of the same paragraph.
- Follow up questions are rewritten into standalone queries using the chat history ("how does it stay balanced?" after asking about AVL trees).
- A grounding check refuses to answer when nothing relevant was retrieved, and points the student to the closest sections instead.
- Answers cite passages as [1], [2] and each citation maps back to a document, page and section.
- Streaming answers over server sent events, plus a plain JSON endpoint.
- Sessions can be restricted to specific documents, exported to Markdown, and each answer can be rated.

**Studying**
- Document summaries in three styles (brief, detailed, bullets). Map reduce with an LLM, or an extractive fallback offline.
- Flashcards generated from the material, reviewed with the SM-2 spaced repetition algorithm. The review queue shows what each button will schedule ("10m", "6d").
- Multiple choice quizzes with plausible distractors. Attempts are graded and feed a per topic mastery table.
- Adaptive quizzes that lean on the topics a student keeps getting wrong.
- Personalized study guides: key concepts with definitions pulled from the notes, extra depth on weak topics, practice questions, and a day by day plan up to the exam date that brings weak topics back for a second pass.
- Progress dashboard: study streak, questions this week, cards due, quiz trend, weakest and strongest topics, token usage.

**Platform**
- JWT auth with short access tokens and single use refresh tokens (revoked in Redis).
- Redis sliding window rate limiting per user (or per IP when anonymous), with separate scopes for auth, uploads and LLM calls.
- A daily token budget per student so one person cannot burn through the API bill.
- Redis caching for query embeddings, answers, search results, topics and stats. Course caches are invalidated with a version counter, so a new upload never serves stale answers.
- Structured JSON logs with request ids, Prometheus metrics, liveness and readiness endpoints.
- Three LLM providers behind one interface: OpenAI, Anthropic models on Amazon Bedrock, and an offline extractive provider that needs no API key.
- A small single page web UI served by Flask for trying everything out.

## Stack

Flask 3, SQLAlchemy 2, PostgreSQL 16 with pgvector, Redis 7, Celery, gunicorn, nginx, Docker. AWS deployment with Terraform: ECS Fargate, RDS, ElastiCache, S3, ALB, Secrets Manager and CloudWatch.

## Running it locally

With Docker:

```bash
cp .env.example .env
make up          # api, worker, beat, postgres, redis, nginx
make seed        # demo account plus three sample lectures
```

Open http://localhost:8080 and log in as `demo@studymate.local` / `demo-pass-2026`.

Without Docker you need Postgres with the pgvector extension and Redis running:

```bash
pip install -r requirements-dev.txt
export DATABASE_URL=postgresql+psycopg2://studymate:studymate@localhost:5432/studymate
export INGEST_ASYNC=false        # process uploads inline instead of through Celery
flask --app wsgi init-db
python scripts/seed_demo.py
make dev
```

The defaults use the offline providers (`LLM_PROVIDER=stub`, `EMBEDDING_PROVIDER=local`), so everything works without an API key. Set `LLM_PROVIDER=openai` and `OPENAI_API_KEY`, or `LLM_PROVIDER=bedrock` with AWS credentials, to use a real model.

## Tests

```bash
make test
```

53 tests cover auth, uploads for each file type, ingestion failures, retrieval, the answer cache and its invalidation, streaming, quotas, rate limits, flashcards, quizzes, study guides and the admin endpoints. They run on SQLite and fakeredis so they need nothing installed. `scripts/smoke_test.py` runs the same flows end to end against a real Postgres and Redis, and CI runs it on every push.

## Load testing

`loadtest/locustfile.py` simulates students asking questions, streaming answers, reviewing flashcards, searching, taking quizzes and checking their dashboard. With 500 concurrent simulated students on a single 2 vCPU machine the platform served about 92 requests per second with no failed requests, a median of 73 ms and a p95 under one second. The full write up, including two problems the test found and how they were fixed, is in [docs/load-test-report.md](docs/load-test-report.md).

## Project layout

```
app/
  __init__.py          application factory
  config.py            all settings, read from environment variables
  models.py            SQLAlchemy models (pgvector column falls back to JSON on SQLite)
  auth/                JWT issuing, revocation, decorators, auth routes
  api/                 blueprints: courses, documents, chat, search, guides, flashcards, quizzes, insights, health
  services/
    extraction.py      PDF, DOCX, PPTX, Markdown and text parsing
    chunking.py        sentence aware chunking
    embeddings.py      local hashing embedder and OpenAI embeddings
    retrieval.py       vector search, keyword search, RRF, MMR
    llm.py             OpenAI, Bedrock and offline providers
    rag.py             the question answering pipeline
    summarizer.py      map reduce and extractive summaries
    generators.py      flashcard and quiz generation, grading
    study_guide.py     personalized guides and study plans
    spaced_repetition.py  SM-2
    rate_limit.py, cache.py, quota.py, storage.py, analytics.py, keywords.py
  tasks.py             Celery tasks
web/index.html         single page UI
deploy/                nginx config, Terraform, deploy script
loadtest/              Locust scenario
scripts/               seeding and smoke test
docs/                  architecture, API, deployment, load test report
```

## Docs

- [Architecture](docs/architecture.md): how a question flows through the system and why things are built the way they are
- [API reference](docs/api.md)
- [Deployment](docs/deployment.md)
- [Load test report](docs/load-test-report.md)
