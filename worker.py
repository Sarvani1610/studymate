"""Celery entrypoint: `celery -A worker.celery worker -Q ingest,default -l info`."""
from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402
from app.tasks import celery  # noqa: E402,F401

flask_app = create_app()
