"""Document ingestion pipeline: storage -> text -> chunks -> embeddings.

Runs inside a Celery worker in production and inline during tests. The
function is idempotent: re-running it for a document replaces its chunks, which
is also how the admin "reindex" command works after changing chunk settings.
"""
import hashlib
import logging
import time

from flask import current_app

from ..extensions import db
from ..metrics import INGEST_JOBS, INGEST_SECONDS
from ..models import Chunk, Document, utcnow
from . import cache
from .chunking import chunk_segments
from .embeddings import get_embedder
from .extraction import ExtractionError, extract, guess_title
from .storage import get_storage

log = logging.getLogger(__name__)

EMBED_BATCH = 64


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def run_ingestion(document_id):
    doc = db.session.get(Document, document_id)
    if doc is None:
        log.warning("ingestion skipped, document %s no longer exists", document_id)
        return None

    started = time.perf_counter()
    doc.status = "processing"
    doc.error = None
    db.session.commit()

    try:
        data = get_storage().get(doc.storage_key)
        segments = extract(data, doc.extension)
        cfg = current_app.config
        drafts = chunk_segments(
            segments,
            target_tokens=cfg["CHUNK_SIZE_TOKENS"],
            overlap_sentences=cfg["CHUNK_OVERLAP_SENTENCES"],
        )
        if not drafts:
            raise ExtractionError("The document did not produce any text chunks")

        embedder = get_embedder()
        vectors = []
        for start in range(0, len(drafts), EMBED_BATCH):
            vectors.extend(embedder.embed([d.text for d in drafts[start:start + EMBED_BATCH]]))

        Chunk.query.filter_by(document_id=doc.id).delete(synchronize_session=False)
        db.session.bulk_save_objects([
            Chunk(
                document_id=doc.id,
                course_id=doc.course_id,
                ordinal=i,
                text=draft.text,
                token_count=draft.token_count,
                page=draft.page,
                section=(draft.section or "")[:255] or None,
                embedding=vector,
            )
            for i, (draft, vector) in enumerate(zip(drafts, vectors, strict=False))
        ])

        pages = {s.page for s in segments if s.page}
        doc.title = doc.title or guess_title(doc.filename, segments)[:255]
        doc.page_count = len(pages) if pages else 0
        doc.chunk_count = len(drafts)
        doc.word_count = sum(len(s.text.split()) for s in segments)
        doc.status = "ready"
        doc.processed_at = utcnow()
        doc.processing_ms = int((time.perf_counter() - started) * 1000)
        db.session.commit()
        cache.bump_course_version(doc.course_id)
        INGEST_JOBS.labels("ok").inc()
        log.info("ingested document %s: %s chunks in %sms", doc.id, doc.chunk_count, doc.processing_ms)
    except ExtractionError as exc:
        _fail(doc, str(exc))
    except Exception as exc:
        log.exception("ingestion crashed for document %s", document_id)
        _fail(doc, "Processing failed unexpectedly. Try uploading the file again.")
        raise exc
    finally:
        INGEST_SECONDS.observe(time.perf_counter() - started)
    return doc


def _fail(doc, message):
    db.session.rollback()
    doc = db.session.get(Document, doc.id)
    if doc is None:
        return
    doc.status = "failed"
    doc.error = message[:1000]
    doc.processed_at = utcnow()
    db.session.commit()
    INGEST_JOBS.labels("failed").inc()
    log.warning("ingestion failed for document %s: %s", doc.id, message)


def dispatch(document_id):
    """Queue ingestion, or run it inline when async is off (tests, local debugging)."""
    if current_app.config.get("INGEST_ASYNC"):
        from ..tasks import ingest_document

        ingest_document.delay(document_id)
        return "queued"
    try:
        run_ingestion(document_id)
    except Exception:
        pass  # already recorded on the document row
    return "inline"
