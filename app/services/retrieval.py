"""Hybrid retrieval: dense vectors plus keyword search, fused with RRF.

Course notes are full of exact terms (algorithm names, theorem numbers, acronyms)
that dense embeddings sometimes blur, while keyword search misses paraphrased
questions. Running both and fusing the ranked lists with reciprocal rank fusion
has held up noticeably better than either alone on my test questions. A final
MMR pass keeps the context from being five near copies of the same paragraph.

Postgres path: pgvector cosine distance plus full text search (ts_rank_cd).
SQLite path (tests): numpy cosine plus an in-memory BM25.
"""
import logging
import math
import time
from collections import Counter
from dataclasses import dataclass, field

import numpy as np
from flask import current_app
from sqlalchemy import Float, String, bindparam, cast, func

from ..extensions import db
from ..metrics import RETRIEVAL_SECONDS
from ..models import Chunk, Document
from ..utils.text import content_words
from .embeddings import embed_query, light_stem

log = logging.getLogger(__name__)
RRF_K = 60


@dataclass
class Hit:
    chunk: Chunk
    score: float = 0.0
    vector_score: float | None = None
    keyword_score: float | None = None
    vector_rank: int | None = None
    keyword_rank: int | None = None
    sources: list = field(default_factory=list)

    def to_dict(self, snippet_len=300):
        from ..utils.text import truncate

        doc = self.chunk.document
        return {
            "chunk_id": self.chunk.id,
            "document_id": self.chunk.document_id,
            "document": (doc.title or doc.filename) if doc else None,
            "filename": doc.filename if doc else None,
            "page": self.chunk.page,
            "section": self.chunk.section,
            "score": round(self.score, 4),
            "vector_score": None if self.vector_score is None else round(self.vector_score, 4),
            "keyword_score": None if self.keyword_score is None else round(self.keyword_score, 4),
            "matched_by": self.sources,
            "snippet": truncate(self.chunk.text, snippet_len),
        }


def _is_postgres():
    return db.engine.dialect.name == "postgresql"


def _scope(query, course_id, document_ids):
    query = query.join(Document, Document.id == Chunk.document_id).filter(
        Chunk.course_id == course_id, Document.status == "ready"
    )
    if document_ids:
        query = query.filter(Chunk.document_id.in_(document_ids))
    return query


# ---------------------------------------------------------------- dense search

def vector_search(course_id, qvec, limit, document_ids=None):
    if _is_postgres():
        from pgvector.sqlalchemy import Vector

        literal = "[" + ",".join(f"{x:.6f}" for x in qvec) + "]"
        qparam = cast(bindparam("qvec", literal, type_=String), Vector(len(qvec)))
        distance = Chunk.embedding.op("<=>", return_type=Float)(qparam)
        rows = (
            _scope(db.session.query(Chunk, distance.label("dist")), course_id, document_ids)
            .filter(Chunk.embedding.isnot(None))
            .order_by(distance)
            .limit(limit)
            .all()
        )
        return [(chunk, 1.0 - float(dist)) for chunk, dist in rows]

    chunks = _scope(Chunk.query, course_id, document_ids).filter(Chunk.embedding.isnot(None)).all()
    if not chunks:
        return []
    matrix = np.array([c.embedding for c in chunks], dtype=np.float32)
    query = np.array(qvec, dtype=np.float32)
    sims = matrix @ query
    order = np.argsort(-sims)[:limit]
    return [(chunks[i], float(sims[i])) for i in order]


# -------------------------------------------------------------- keyword search

class BM25:
    def __init__(self, docs, k1=1.4, b=0.75):
        self.k1 = k1
        self.b = b
        self.docs = [Counter(d) for d in docs]
        self.lengths = [len(d) for d in docs]
        self.avgdl = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        df = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def score(self, query_terms):
        scores = []
        for tf, length in zip(self.docs, self.lengths, strict=False):
            s = 0.0
            for term in query_terms:
                f = tf.get(term)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * length / (self.avgdl or 1))
                s += self.idf.get(term, 0.0) * f * (self.k1 + 1) / denom
            scores.append(s)
        return scores


def _terms(text):
    return [light_stem(w) for w in content_words(text)]


def keyword_search(course_id, query_text, limit, document_ids=None):
    words = content_words(query_text)
    if not words:
        return []
    if _is_postgres():
        tsquery = func.to_tsquery("english", " | ".join(sorted(set(words))))
        vector = func.to_tsvector("english", Chunk.text)
        rank = func.ts_rank_cd(vector, tsquery, 32)
        rows = (
            _scope(db.session.query(Chunk, rank.label("rank")), course_id, document_ids)
            .filter(vector.op("@@")(tsquery))
            .order_by(rank.desc())
            .limit(limit)
            .all()
        )
        return [(chunk, float(r)) for chunk, r in rows]

    chunks = _scope(Chunk.query, course_id, document_ids).all()
    if not chunks:
        return []
    bm25 = BM25([_terms(c.text) for c in chunks])
    scores = bm25.score(_terms(query_text))
    ranked = sorted(zip(chunks, scores, strict=False), key=lambda p: p[1], reverse=True)
    return [(c, s) for c, s in ranked[:limit] if s > 0]


# ------------------------------------------------------------------- fusion

def reciprocal_rank_fusion(dense, sparse, dense_weight=1.0, sparse_weight=1.0):
    hits = {}
    for rank, (chunk, score) in enumerate(dense, start=1):
        hit = hits.setdefault(chunk.id, Hit(chunk=chunk))
        hit.vector_score, hit.vector_rank = score, rank
        hit.score += dense_weight / (RRF_K + rank)
        hit.sources.append("vector")
    for rank, (chunk, score) in enumerate(sparse, start=1):
        hit = hits.setdefault(chunk.id, Hit(chunk=chunk))
        hit.keyword_score, hit.keyword_rank = score, rank
        hit.score += sparse_weight / (RRF_K + rank)
        hit.sources.append("keyword")
    best = (dense_weight + sparse_weight) / (RRF_K + 1)
    for hit in hits.values():
        hit.score = hit.score / best
    return sorted(hits.values(), key=lambda h: h.score, reverse=True)


def mmr(hits, qvec, k, lam=0.7):
    """Maximal marginal relevance over the fused candidates."""
    pool = [h for h in hits if h.chunk.embedding]
    if len(pool) <= k:
        return hits[:k]
    vectors = np.array([h.chunk.embedding for h in pool], dtype=np.float32)
    query = np.array(qvec, dtype=np.float32)
    relevance = np.array([h.score for h in pool]) * 0.5 + (vectors @ query) * 0.5
    selected = []
    remaining = list(range(len(pool)))
    while remaining and len(selected) < k:
        if not selected:
            best = max(remaining, key=lambda i: relevance[i])
        else:
            # Similarity of every remaining candidate to its closest already chosen passage.
            redundancy = (vectors[remaining] @ vectors[selected].T).max(axis=1)
            scores = lam * relevance[remaining] - (1 - lam) * redundancy
            best = remaining[int(np.argmax(scores))]
        selected.append(best)
        remaining.remove(best)
    return [pool[i] for i in selected]


def retrieve(course_id, query_text, k=None, document_ids=None, use_mmr=True):
    cfg = current_app.config
    k = k or cfg["RETRIEVAL_TOP_K"]
    candidates = cfg["RETRIEVAL_CANDIDATES"]
    started = time.perf_counter()

    qvec = embed_query(query_text)
    dense = vector_search(course_id, qvec, candidates, document_ids)
    sparse = keyword_search(course_id, query_text, candidates, document_ids)
    fused = reciprocal_rank_fusion(dense, sparse)
    top = mmr(fused, qvec, k, cfg["MMR_LAMBDA"]) if use_mmr else fused[:k]

    RETRIEVAL_SECONDS.observe(time.perf_counter() - started)
    log.debug("retrieval course=%s dense=%s sparse=%s returned=%s", course_id, len(dense), len(sparse), len(top))
    return top, qvec


def is_grounded(hits, min_score):
    """Decide whether we have enough evidence to answer at all."""
    if not hits:
        return False
    best = hits[0]
    if best.keyword_score and best.keyword_score > 0:
        return True
    return (best.vector_score or 0.0) >= min_score
