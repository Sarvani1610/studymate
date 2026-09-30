"""Application factory."""
import logging
import os

from flask import Flask, jsonify
from sqlalchemy import event
from sqlalchemy.engine import Engine

from .config import get_config
from .errors import register_error_handlers
from .extensions import db, init_redis
from .logging_setup import configure_logging, install_request_hooks

log = logging.getLogger(__name__)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    # SQLite ignores ON DELETE CASCADE unless foreign keys are switched on per connection.
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_app(config_name=None, overrides=None):
    config = get_config(config_name)
    if hasattr(config, "validate"):
        problems = config.validate()
        if problems:
            raise RuntimeError("Invalid production config: " + "; ".join(problems))

    app = Flask(__name__, static_folder="../web", static_url_path="/static")
    app.config.from_object(config)
    app.config["APP_VERSION"] = os.getenv("APP_VERSION", "dev")
    app.config["MAX_CONTENT_LENGTH"] = (config.MAX_UPLOAD_MB + 1) * 1024 * 1024
    if overrides:
        app.config.update(overrides)

    configure_logging(app)
    db.init_app(app)
    init_redis(app)
    install_request_hooks(app)
    register_error_handlers(app)

    from .services.rate_limit import install_default_limit

    install_default_limit(app)

    from .api import chat, courses, documents, flashcards, guides, health, insights, quizzes, search
    from .auth import routes as auth_routes

    for module in (health, auth_routes, courses, documents, chat, search, guides, flashcards, quizzes, insights):
        app.register_blueprint(module.bp)

    from .tasks import init_celery

    init_celery(app)

    from .cli import register_cli

    register_cli(app)

    @app.get("/")
    def index():
        if app.static_folder and os.path.exists(os.path.join(app.static_folder, "index.html")):
            return app.send_static_file("index.html")
        return jsonify({"name": app.config["APP_NAME"], "docs": "/api/version"})

    @app.before_request
    def preflight():
        from flask import request

        if request.method == "OPTIONS":
            return "", 204
        return None

    if app.config.get("TESTING") or app.config.get("AUTO_CREATE_TABLES"):
        with app.app_context():
            db.create_all()

    log.info("app started env=%s llm=%s embeddings=%s", config.ENV_NAME,
             app.config["LLM_PROVIDER"], app.config["EMBEDDING_PROVIDER"])
    return app
