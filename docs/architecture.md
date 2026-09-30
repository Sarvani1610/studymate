# Architecture

## The pieces

```
            browser / web UI
                   |
                 nginx  (TLS, request ids, no buffering for streams)
                   |
        gunicorn + Flask API  (3 to 6 tasks on ECS)
         |        |         |
   Postgres    Redis      S3
   pgvector    cache      uploaded files
   FTS index   rate limits
               token quotas
               Celery broker
                   |
             Celery worker  (ingestion)
                   |
        LLM provider (OpenAI, Bedrock, or offline)
```

The API is stateless. Everything that has to be shared between processes lives in Postgres (durable data), Redis (anything short lived) or S3 (files). That is what makes it safe to run several API tasks behind the load balancer and scale them on CPU.

## Uploading a document

1. The API checks the extension, the file signature (a `.pdf` has to start with `%PDF`), the size, and the SHA-256. If the same file already exists in the course it returns the existing document instead of processing it again.
2. The file goes to S3 under `courses/<course_id>/<sha256>.<ext>` and a `documents` row is created with status `pending`.
3. A Celery task picks it up on the `ingest` queue:
   - extract text per page (PDF), per slide including speaker notes (PPTX), or per heading (DOCX and Markdown),
   - drop lines that repeat on most pages, which are almost always headers, footers and page numbers,
   - split into sentences and pack them into chunks of about 220 tokens with one sentence of overlap, never crossing a section heading,
   - embed the chunks in batches of 64 and bulk insert them,
   - bump the course cache version so cached answers for that course stop being used.
4. The document moves to `ready`, or to `failed` with a message a student can act on ("No readable text found. If this is a scanned PDF, run it through OCR first").

A beat task requeues documents stuck in `processing` for more than 20 minutes, which covers a worker dying halfway through a job. Tasks use `acks_late` so the broker only forgets a job once it finished.

## Answering a question

This is `app/services/rag.py`.

1. **Rewrite.** If the session already has messages, the question is rewritten into a standalone query using the recent history. With a real model this is a short LLM call. Offline, short or pronoun heavy questions borrow keywords from the previous question.
2. **Cache.** Questions that did not need rewriting are looked up in Redis. The key includes the course cache version, the session's document filter, the student's level, the model name and the normalized question. Rewritten questions are never cached because their meaning depends on the conversation.
3. **Retrieve.** Two searches run over the course's chunks:
   - dense: pgvector cosine distance on the query embedding, using an HNSW index,
   - sparse: Postgres full text search with `ts_rank_cd`, using a GIN index on `to_tsvector('english', text)`.
   The two ranked lists are merged with reciprocal rank fusion (k = 60). RRF only looks at ranks, so the two very different score scales never need to be normalized against each other. The top candidates then go through MMR, which trades a bit of relevance for less overlap between the chosen passages.
4. **Budget.** Passages are added in order until the context token budget (2,500 tokens by default) is full.
5. **Grounding check.** If the best passage has no keyword match and a low vector score, the system says it could not find the answer in the course material and lists the closest sections. This is cheaper than asking the model and it avoids the model making something up from general knowledge.
6. **Generate.** The prompt numbers each passage and tells the model to cite them like [2]. The student's level (intro, intermediate, advanced) changes the tone instructions.
7. **Cite.** The `[n]` markers in the answer are mapped back to chunks, so each citation has the file name, page, section and a snippet. Markers that do not match a passage are ignored.
8. **Record.** Both messages are saved, a usage event is written, tokens are added to the student's daily quota in Redis, and the answer is cached if it had citations.

The streaming endpoint does the same work but sends a `meta` event with the sources first, then `token` events as the model writes, then a `done` event with the saved message. The generator reloads the user and session by id instead of reusing objects from the view, because by the time the response body streams those objects can be detached from the database session. That bug only showed up under gunicorn, not in the Flask test client.

## Why hybrid retrieval

Course notes are full of exact terms: algorithm names, theorem numbers, acronyms like AVL or BFS. Dense embeddings sometimes rank a paragraph about a related concept above the one that actually uses the term. Keyword search catches those, but misses questions phrased differently from the notes ("why is quicksort slow on sorted input" against a paragraph that says "degrades to quadratic time"). Running both and fusing the rankings handled both kinds of question in my test set, and the fusion step costs almost nothing.

## The offline providers

Two providers need no network at all:

- **Local embeddings** use the hashing trick on stemmed words, word pairs and character four grams, with sublinear term frequency and L2 normalization, into 384 dimensions. They are nowhere near a trained model on paraphrases, but on lecture notes, where questions reuse the vocabulary of the material, they work reasonably well, and they make tests deterministic.
- **The stub LLM** answers extractively: it scores sentences from the retrieved passages by overlap with the question, gives a bonus to sentences that define a term from the question, keeps only sentences close to the best score, and cites them. Flashcards come from definition sentences ("A heap is ...") and cloze deletions. Quizzes are cloze questions whose distractors are other key phrases from the same course with the same number of words as the answer.

The rest of the code does not know which provider is active. Each generator checks one flag, `llm.generative`, to decide between the JSON prompt and the heuristic.

## Topics and key concepts

Topics are extracted with TF-IDF over the course's chunks. Candidates are one to three word phrases that do not start or end with a stopword and do not cross a sentence boundary. Plurals are folded so "trees" and "tree" count together. Two rules made the biggest difference in quality:

- a phrase gets full weight only if it follows an article somewhere in the text ("a binary heap", "the load factor"). That filters out verb phrases like "subtree holds",
- when a longer phrase appears almost everywhere a shorter one does, the longer one wins ("binary search tree" over "search tree").

Topics label quiz questions, which is what the mastery table and the adaptive features are built on.

## Personalization

- Every graded quiz question updates a row in `topic_mastery` for that student, course and topic.
- A topic with at least two attempts and under 70 percent accuracy counts as weak.
- Adaptive quizzes pull chunks that mention weak topics first.
- Study guides explain weak topics in more depth and schedule each one twice: once in the normal rotation and again three days later, since spaced review sticks better than a single pass. The last day before the exam is always a full review.
- Flashcards use SM-2, so cards a student keeps missing come back in 10 minutes and cards they know well spread out to weeks.

## Caching

| What | Key includes | TTL |
|---|---|---|
| Query embeddings | provider, dimension, lowercased query | 24 h |
| Answers | course version, document filter, level, model, normalized question | 1 h |
| Search results | course version, k, filter, query | 10 min |
| Course topics | course version, limit | 1 h |
| Progress stats | user, course | 60 s |
| Summaries | stored in Postgres per document version and style | until reprocessed |

Invalidation is by version counter. Uploading, reprocessing or deleting a document increments `cache:course_ver:<id>`, so every key built afterwards is different and the old ones expire on their own. There is no key scanning on the hot path. If Redis cannot be reached, `course_version` returns -1 so nothing stale can be served, and the rest of the cache helpers just log and carry on.

## Rate limiting and quotas

The limiter is a sliding window on a Redis sorted set: trim entries older than the window, add this request, count, all in one pipelined round trip. It is more accurate than fixed windows, which let a client send double the limit around the window boundary.

Scopes and defaults:

| Scope | Default | Applies to |
|---|---|---|
| default | 120 per minute | every API request |
| llm | 20 per minute | answers, summaries, quizzes, flashcards, guides |
| auth | 10 per minute | register, login, refresh, password change |
| upload | 30 per hour | uploads and reprocessing |

Requests are counted per user. The global limit runs before the auth decorator has loaded the user, so the limiter reads the subject from the bearer token itself (after verifying the signature). The first version keyed everything by IP until the auth decorator ran, which the load test caught: 500 students behind one address shared a single bucket. Anonymous requests still count per IP.

On top of that each student has a daily token budget (200,000 by default) tracked with `INCRBY` on a key that expires after a day. Admins are exempt.

The limiter fails open when Redis is down. For a study tool that is a better outcome than every student getting errors.

## Observability

- Every request gets an id, from `X-Request-ID` if the load balancer set one. It is in every log line and in every error response, so a student's bug report can be matched to logs.
- Logs are JSON in production with method, route, status, latency and user id. Requests over a threshold are logged at warning level as slow.
- Prometheus metrics cover request counts and latency by route, LLM calls, tokens and latency by provider and task, cache hits and misses by cache, rate limit rejections, ingestion outcomes and durations, retrieval time and open streams. Gunicorn runs in multiprocess mode so all workers report together.
- `/health/live` only says the process is up and is used by the container health check. `/health/ready` checks Postgres and Redis and is what the load balancer uses, so a task that loses its database connection is taken out of rotation instead of restarted.

## Things I would do next

- Replace `create_all` with Alembic migrations.
- A cross encoder reranker on the top 20 fused results. It would help the offline embeddings most.
- OCR for scanned PDFs with Textract, since a lot of older course material is scanned.
- Sharing a course between students in a study group, which needs a membership table and changes to every ownership check.
- Persist a partial answer when a client disconnects halfway through a stream. Right now that answer is lost.
