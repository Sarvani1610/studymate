"""Question answering over a course (the RAG pipeline).

Steps for each question:
 1. Rewrite follow ups into a standalone query using recent history.
 2. Check the answer cache (keyed on course version, filters, level, question).
 3. Hybrid retrieval, then trim passages to the context token budget.
 4. Grounding check: if nothing relevant came back, say so instead of guessing.
 5. Call the LLM with numbered passages and map its [n] markers to citations.
 6. Persist both messages, usage, token quota, and cache the answer.
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field

from flask import current_app

from ..extensions import db
from ..metrics import ACTIVE_STREAMS
from ..models import Message, UsageEvent, utcnow
from ..utils.text import content_words, estimate_tokens, normalize_question, truncate
from . import cache, quota
from .llm import NO_ANSWER, get_llm
from .prompts import (
    ANSWER_SYSTEM,
    ANSWER_USER,
    LEVEL_HINTS,
    REWRITE_SYSTEM,
    REWRITE_USER,
    format_context,
    format_history,
)
from .retrieval import is_grounded, retrieve

log = logging.getLogger(__name__)
PRONOUNS = re.compile(r"\b(it|this|that|they|those|these|its|them|he|she|the former|the latter)\b", re.I)


@dataclass
class Prepared:
    question: str
    search_query: str
    hits: list
    grounded: bool
    system: str
    prompt: str
    cache_key: str | None
    cached: dict | None
    rewrite_tokens: int = 0
    started: float = field(default_factory=time.perf_counter)


def _history(session):
    turns = current_app.config["HISTORY_TURNS"]
    return session.messages[-turns * 2:] if session.messages else []


def rewrite_query(llm, question, history):
    if not history:
        return question, 0
    if llm.generative:
        try:
            result = llm.complete(
                REWRITE_SYSTEM,
                REWRITE_USER.format(history=format_history(history), question=question),
                task="rewrite", max_tokens=60, temperature=0.0,
            )
            rewritten = result.text.strip().strip('"').strip()
            return (rewritten or question), result.tokens_in + result.tokens_out
        except Exception:
            log.warning("query rewrite failed, using the raw question")
            return question, 0
    # Offline heuristic: short or pronoun heavy follow ups borrow keywords from the previous question.
    if len(content_words(question)) < 4 or PRONOUNS.search(question):
        previous = next((m.content for m in reversed(history) if m.role == "user"), "")
        extra = [w for w in content_words(previous) if w not in content_words(question)][:8]
        if extra:
            return f"{question} {' '.join(extra)}", 0
    return question, 0


def _trim_to_budget(hits, budget):
    kept, used = [], 0
    for hit in hits:
        cost = hit.chunk.token_count or estimate_tokens(hit.chunk.text)
        if kept and used + cost > budget:
            break
        kept.append(hit)
        used += cost
    return kept


def extract_citations(answer, hits):
    cited, seen = [], set()
    for match in re.finditer(r"\[(\d+)\]", answer):
        number = int(match.group(1))
        if 1 <= number <= len(hits) and number not in seen:
            seen.add(number)
            item = hits[number - 1].to_dict(snippet_len=240)
            item["marker"] = number
            cited.append(item)
    return cited


def _cache_key(session, user, question, search_query):
    if search_query != question:
        return None  # depends on conversation history, not safe to share
    return cache.make_key(
        "answer",
        session.course_id,
        cache.course_version(session.course_id),
        ",".join(str(d) for d in sorted(session.document_filter or [])),
        user.study_level,
        current_app.config["LLM_MODEL"],
        normalize_question(question),
    )


def prepare(user, session, question):
    cfg = current_app.config
    llm = get_llm()
    history = _history(session)
    search_query, rewrite_tokens = rewrite_query(llm, question, history)

    key = _cache_key(session, user, question, search_query)
    cached = cache.get_json(key, "answer") if key else None
    if cached:
        return Prepared(question, search_query, [], True, "", "", key, cached, rewrite_tokens)

    hits, _ = retrieve(session.course_id, search_query, document_ids=session.document_filter or None)
    hits = _trim_to_budget(hits, cfg["CONTEXT_TOKEN_BUDGET"])
    grounded = is_grounded(hits, cfg["RETRIEVAL_MIN_SCORE"])

    system = ANSWER_SYSTEM.format(level_hint=LEVEL_HINTS.get(user.study_level, ""))
    prompt = ANSWER_USER.format(context=format_context(hits), history=format_history(history), question=question)
    return Prepared(question, search_query, hits, grounded, system, prompt, key, None, rewrite_tokens)


def _no_answer_text(hits):
    sections = []
    for hit in hits[:3]:
        label = hit.chunk.section or (hit.chunk.document.title if hit.chunk.document else None)
        if label and label not in sections:
            sections.append(label)
    if sections:
        return NO_ANSWER + " Closest sections I found: " + "; ".join(sections) + "."
    return NO_ANSWER


def _persist(user, session, prep, answer_text, citations, tokens_in, tokens_out, model, cached, grounded):
    latency_ms = int((time.perf_counter() - prep.started) * 1000)
    user_msg = Message(session_id=session.id, role="user", content=prep.question)
    bot_msg = Message(
        session_id=session.id,
        role="assistant",
        content=answer_text,
        citations=citations,
        rewritten_query=prep.search_query if prep.search_query != prep.question else None,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        cached=cached,
        grounded=grounded,
    )
    db.session.add_all([user_msg, bot_msg])
    if not session.messages or session.title == "New session":
        session.title = truncate(prep.question, 80)
    session.updated_at = utcnow()
    db.session.add(UsageEvent(
        user_id=user.id, course_id=session.course_id, kind="chat", tokens_in=tokens_in,
        tokens_out=tokens_out, latency_ms=latency_ms, cached=cached, model=model,
    ))
    db.session.commit()
    quota.record(user.id, tokens_in + tokens_out + prep.rewrite_tokens)
    return user_msg, bot_msg


def answer(user, session, question):
    prep = prepare(user, session, question)
    if prep.cached:
        cached = prep.cached
        _, bot = _persist(user, session, prep, cached["answer"], cached["citations"], 0, 0, "cache", True, True)
        return bot

    if not prep.grounded:
        _, bot = _persist(user, session, prep, _no_answer_text(prep.hits), [], 0, 0, "none", False, False)
        return bot

    quota.ensure_budget(user, estimate_tokens(prep.system + prep.prompt))
    llm = get_llm()
    result = llm.complete(prep.system, prep.prompt, task="answer")
    citations = extract_citations(result.text, prep.hits)
    grounded = bool(citations) or result.text.strip() != NO_ANSWER
    _, bot = _persist(user, session, prep, result.text, citations, result.tokens_in, result.tokens_out,
                      result.model, False, grounded)
    if prep.cache_key and citations:
        cache.set_json(prep.cache_key, {"answer": result.text, "citations": citations},
                       current_app.config["CACHE_TTL_ANSWER"])
    return bot


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def answer_stream(user_id, session_id, question):
    """Generator of server sent events. Must run inside stream_with_context.

    Takes ids rather than ORM objects: by the time the response body starts
    streaming, objects loaded in the view can be detached from the session,
    so everything is loaded fresh here.
    """
    from ..models import ChatSession, User

    ACTIVE_STREAMS.inc()
    try:
        user = db.session.get(User, user_id)
        session = db.session.get(ChatSession, session_id)
        prep = prepare(user, session, question)
        if prep.cached:
            yield _sse("meta", {"cached": True, "citations": prep.cached["citations"], "grounded": True})
            for piece in re.findall(r"\S+\s*", prep.cached["answer"]):
                yield _sse("token", {"t": piece})
            _, bot = _persist(user, session, prep, prep.cached["answer"], prep.cached["citations"], 0, 0,
                              "cache", True, True)
            yield _sse("done", {"message": bot.to_dict()})
            return

        if not prep.grounded:
            text = _no_answer_text(prep.hits)
            yield _sse("meta", {"cached": False, "citations": [], "grounded": False})
            yield _sse("token", {"t": text})
            _, bot = _persist(user, session, prep, text, [], 0, 0, "none", False, False)
            yield _sse("done", {"message": bot.to_dict()})
            return

        quota.ensure_budget(user, estimate_tokens(prep.system + prep.prompt))
        yield _sse("meta", {
            "cached": False,
            "grounded": True,
            "sources": [h.to_dict(snippet_len=160) for h in prep.hits],
            "search_query": prep.search_query,
        })
        llm = get_llm()
        sink = {}
        for piece in llm.stream(prep.system, prep.prompt, task="answer", out=sink):
            yield _sse("token", {"t": piece})
        result = sink.get("result")
        text = result.text if result else ""
        citations = extract_citations(text, prep.hits)
        _, bot = _persist(user, session, prep, text, citations,
                          result.tokens_in if result else 0, result.tokens_out if result else 0,
                          result.model if result else "unknown", False, bool(citations))
        if prep.cache_key and citations:
            cache.set_json(prep.cache_key, {"answer": text, "citations": citations},
                           current_app.config["CACHE_TTL_ANSWER"])
        yield _sse("done", {"message": bot.to_dict()})
    except Exception as exc:
        db.session.rollback()
        log.exception("stream failed")
        from ..errors import APIError

        if isinstance(exc, APIError):
            yield _sse("error", {"message": exc.message, "code": exc.code})
        else:
            yield _sse("error", {"message": "Something went wrong while answering", "code": "internal_error"})
    finally:
        ACTIVE_STREAMS.dec()
