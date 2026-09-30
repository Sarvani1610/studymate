"""Embedding providers.

`local` is a hashing embedder: no network, no model download, deterministic,
and good enough for keyword heavy course material. It is what the tests and
the offline demo use. `openai` calls the embeddings API with the dimensions
parameter so the pgvector column size stays the same no matter which provider
produced the vectors.
"""
import hashlib
import logging
import math
import time
from collections import Counter

from flask import current_app

from ..utils.text import STOPWORDS, tokenize
from . import cache

log = logging.getLogger(__name__)

_SUFFIXES = ("ational", "ization", "ations", "ation", "ments", "ment", "ness", "ities", "ity",
             "ings", "ing", "ies", "ied", "ers", "er", "ed", "ly", "es", "s")


def light_stem(word):
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            if suffix in ("ies", "ied"):
                return word[: -len(suffix)] + "y"
            return word[: -len(suffix)]
    return word


def _l2(vec):
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


class LocalHashEmbedder:
    name = "local-hash"

    def __init__(self, dim=384):
        self.dim = dim

    def _bucket(self, feature):
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "little")
        index = value % self.dim
        sign = 1.0 if (value >> 63) & 1 else -1.0
        return index, sign

    def _features(self, text):
        words = [light_stem(w) for w in tokenize(text) if w not in STOPWORDS and len(w) > 1]
        feats = Counter()
        for w in words:
            feats["w:" + w] += 1.0
        for a, b in zip(words, words[1:], strict=False):
            feats["b:" + a + "_" + b] += 0.7
        for w in words:
            if len(w) >= 6:
                for i in range(len(w) - 3):
                    feats["c:" + w[i:i + 4]] += 0.15
        return feats

    def embed_one(self, text):
        vec = [0.0] * self.dim
        for feature, tf in self._features(text).items():
            index, sign = self._bucket(feature)
            vec[index] += sign * (1.0 + math.log(tf)) if tf >= 1 else sign * tf
        return _l2(vec)

    def embed(self, texts):
        return [self.embed_one(t) for t in texts]


class OpenAIEmbedder:
    name = "openai"

    def __init__(self, model, dim, api_key, batch_size=64):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, timeout=30, max_retries=0)
        self.model = model
        self.dim = dim
        self.batch_size = batch_size

    def embed(self, texts):
        out = []
        for start in range(0, len(texts), self.batch_size):
            batch = [t.replace("\n", " ")[:8000] for t in texts[start:start + self.batch_size]]
            out.extend(self._call(batch))
        return out

    def _call(self, batch, attempts=5):
        delay = 1.0
        for attempt in range(1, attempts + 1):
            try:
                resp = self.client.embeddings.create(model=self.model, input=batch, dimensions=self.dim)
                return [_l2(list(item.embedding)) for item in resp.data]
            except Exception as exc:
                if attempt == attempts:
                    raise
                log.warning("embedding call failed (attempt %s): %s", attempt, exc)
                time.sleep(delay)
                delay = min(delay * 2, 20)
        return []

    def embed_one(self, text):
        return self.embed([text])[0]


def get_embedder():
    app = current_app
    embedder = app.extensions.get("embedder")
    if embedder is None:
        provider = app.config["EMBEDDING_PROVIDER"]
        dim = app.config["EMBEDDING_DIM"]
        if provider == "openai":
            embedder = OpenAIEmbedder(app.config["EMBEDDING_MODEL"], dim, app.config["OPENAI_API_KEY"])
        else:
            embedder = LocalHashEmbedder(dim)
        app.extensions["embedder"] = embedder
    return embedder


def embed_query(text):
    """Query embeddings are cached because students ask the same thing a lot,
    especially right before an exam."""
    embedder = get_embedder()
    key = cache.make_key("qemb", embedder.name, current_app.config["EMBEDDING_DIM"], text.strip().lower())
    hit = cache.get_json(key, "query_embedding")
    if hit is not None:
        return hit
    vector = embedder.embed_one(text)
    cache.set_json(key, vector, current_app.config["CACHE_TTL_EMBEDDING"])
    return vector


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b, strict=False))
