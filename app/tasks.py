"""Celery tasks.

The worker imports `celery` from here after `init_celery(app)` has bound it to
a Flask app, so every task runs inside an application context and can use the
same models and services as the API.
"""
import logging
from datetime import timedelta

from celery import Celery, Task

log = logging.getLogger(__name__)

celery = Celery("studymate")


def init_celery(app):
    class AppContextTask(Task):
        abstract = True

        def __call__(self, *args, **kwargs):
            with app.app_context():
                return super().__call__(*args, **kwargs)

    celery.conf.update(
        broker_url=app.config["CELERY_BROKER_URL"],
        result_backend=app.config["CELERY_RESULT_BACKEND"],
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_time_limit=900,
        task_soft_time_limit=840,
        result_expires=3600,
        task_default_queue="default",
        task_routes={"studymate.ingest_document": {"queue": "ingest"}},
        beat_schedule={
            "cleanup-stale-processing": {
                "task": "studymate.requeue_stale_documents",
                "schedule": timedelta(minutes=10),
            },
        },
        task_always_eager=app.config.get("TESTING", False),
    )
    celery.Task = AppContextTask
    return celery


@celery.task(name="studymate.ingest_document", bind=True, autoretry_for=(ConnectionError,),
             retry_backoff=True, retry_kwargs={"max_retries": 3})
def ingest_document(self, document_id):
    from .services.ingestion import run_ingestion

    doc = run_ingestion(document_id)
    return {"document_id": document_id, "status": doc.status if doc else "missing"}


@celery.task(name="studymate.requeue_stale_documents")
def requeue_stale_documents():
    """Documents stuck in processing (worker died mid job) get put back on the queue."""
    from .extensions import db
    from .models import Document, utcnow

    cutoff = utcnow() - timedelta(minutes=20)
    stale = Document.query.filter(
        Document.status.in_(("processing", "pending")),
        Document.created_at < cutoff,
        (Document.processed_at.is_(None)) | (Document.processed_at < cutoff),
    ).all()
    for doc in stale:
        doc.status = "pending"
    db.session.commit()
    for doc in stale:
        ingest_document.delay(doc.id)
    if stale:
        log.info("requeued %s stale documents", len(stale))
    return len(stale)
