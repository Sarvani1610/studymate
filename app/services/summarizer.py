"""Document summaries.

With a generative model this is map reduce: summarize windows of chunks, then
merge the partial summaries. Offline (stub provider) it falls back to an
extractive summary that scores sentences by similarity to the document
centroid, a cheap version of TextRank that works fine on lecture notes.
"""
import logging

import numpy as np

from ..extensions import db
from ..models import Chunk, Summary, UsageEvent
from ..utils.text import split_sentences
from . import quota
from .embeddings import get_embedder
from .llm import get_llm
from .prompts import STYLE_HINTS, SUMMARY_MAP_SYSTEM, SUMMARY_MAP_USER, SUMMARY_REDUCE_USER

log = logging.getLogger(__name__)
WINDOW_TOKENS = 2400

STYLE_SENTENCES = {"brief": 5, "detailed": 14, "bullets": 10}


def _windows(chunks, budget=WINDOW_TOKENS):
    window, used = [], 0
    for chunk in chunks:
        if window and used + chunk.token_count > budget:
            yield window
            window, used = [], 0
        window.append(chunk)
        used += chunk.token_count
    if window:
        yield window


def extractive_summary(texts, n_sentences, style):
    sentences = []
    for text in texts:
        for s in split_sentences(text):
            if 40 <= len(s) <= 400:
                sentences.append(s)
    if not sentences:
        return ""
    if len(sentences) <= n_sentences:
        picked = sentences
    else:
        vectors = np.array(get_embedder().embed(sentences), dtype=np.float32)
        centroid = vectors.mean(axis=0)
        norm = np.linalg.norm(centroid) or 1.0
        relevance = vectors @ (centroid / norm)
        # Light position prior: intros and section openers tend to carry the main idea.
        position = np.array([1.0 / (1 + 0.02 * i) for i in range(len(sentences))])
        scores = relevance * 0.85 + position * 0.15
        chosen = []
        for index in np.argsort(-scores):
            if len(chosen) >= n_sentences:
                break
            if chosen and float(np.max(vectors[chosen] @ vectors[index])) > 0.85:
                continue  # near duplicate of something already picked
            chosen.append(int(index))
        picked = [sentences[i] for i in sorted(chosen)]
    if style == "bullets":
        return "\n".join(f"- {s}" for s in picked)
    if style == "detailed":
        paragraphs = [" ".join(picked[i:i + 4]) for i in range(0, len(picked), 4)]
        return "\n\n".join(paragraphs)
    return " ".join(picked)


def summarize_document(user, doc, style="brief", force=False):
    existing = Summary.query.filter_by(document_id=doc.id, style=style, doc_version=doc.version).first()
    if existing and not force:
        return existing, True

    chunks = Chunk.query.filter_by(document_id=doc.id).order_by(Chunk.ordinal).all()
    llm = get_llm()
    title = doc.title or doc.filename
    tokens_in = tokens_out = 0

    if llm.generative:
        hint = STYLE_HINTS[style]
        partials = []
        for window in _windows(chunks):
            text = "\n\n".join(c.text for c in window)
            quota.ensure_budget(user, len(text) // 4)
            result = llm.complete(SUMMARY_MAP_SYSTEM, SUMMARY_MAP_USER.format(title=title, style_hint=hint, text=text),
                                  task="summary_map")
            partials.append(result.text)
            tokens_in += result.tokens_in
            tokens_out += result.tokens_out
        if len(partials) == 1:
            content = partials[0]
        else:
            result = llm.complete(
                SUMMARY_MAP_SYSTEM,
                SUMMARY_REDUCE_USER.format(title=title, style_hint=hint, text="\n\n".join(partials)),
                task="summary_reduce",
            )
            content = result.text
            tokens_in += result.tokens_in
            tokens_out += result.tokens_out
        model = llm.model
    else:
        content = extractive_summary([c.text for c in chunks], STYLE_SENTENCES[style], style)
        model = "extractive"

    if existing:
        existing.content = content
        existing.model = model
        summary = existing
    else:
        summary = Summary(document_id=doc.id, style=style, doc_version=doc.version, content=content, model=model)
        db.session.add(summary)
    db.session.add(UsageEvent(user_id=user.id, course_id=doc.course_id, kind="summary",
                              tokens_in=tokens_in, tokens_out=tokens_out, model=model))
    db.session.commit()
    quota.record(user.id, tokens_in + tokens_out)
    return summary, False
