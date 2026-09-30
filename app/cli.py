"""Flask CLI commands: `flask --app wsgi <command>`."""
import click
from flask import current_app
from sqlalchemy import text

from .extensions import db
from .models import Course, Document, User


def register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        """Create extensions, tables and search indexes."""
        if db.engine.dialect.name == "postgresql":
            with db.engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        db.create_all()
        if db.engine.dialect.name == "postgresql":
            dim = current_app.config["EMBEDDING_DIM"]
            with db.engine.begin() as conn:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_chunks_embedding_hnsw ON chunks "
                    "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
                ))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_chunks_text_fts ON chunks "
                    "USING gin (to_tsvector('english', text))"
                ))
            click.echo(f"postgres ready (vector dim {dim})")
        click.echo("tables created")

    @app.cli.command("create-admin")
    @click.option("--email", required=True)
    @click.option("--password", required=True)
    @click.option("--name", default="Admin")
    def create_admin(email, password, name):
        """Create an admin user or promote an existing one."""
        user = User.query.filter_by(email=email.lower()).first()
        if user is None:
            user = User(email=email.lower(), display_name=name)
            db.session.add(user)
        user.role = "admin"
        user.set_password(password)
        db.session.commit()
        click.echo(f"admin ready: {user.email}")

    @app.cli.command("reindex")
    @click.option("--course-id", type=int, default=None)
    def reindex(course_id):
        """Re-run ingestion for one course or everything (after changing chunk settings)."""
        from .services.ingestion import run_ingestion

        query = Document.query
        if course_id:
            query = query.filter_by(course_id=course_id)
        docs = query.all()
        with click.progressbar(docs, label="reindexing") as bar:
            for doc in bar:
                doc.version += 1
                db.session.commit()
                try:
                    run_ingestion(doc.id)
                except Exception as exc:
                    click.echo(f"\ndocument {doc.id} failed: {exc}")
        click.echo(f"done: {len(docs)} documents")

    @app.cli.command("purge-cache")
    @click.argument("namespace")
    def purge_cache(namespace):
        from .services.cache import purge_namespace

        click.echo(f"removed {purge_namespace(namespace)} keys")

    @app.cli.command("stats")
    def stats():
        click.echo(f"users={User.query.count()} courses={Course.query.count()} documents={Document.query.count()}")
