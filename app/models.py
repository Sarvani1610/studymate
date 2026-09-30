"""Database models.

Embeddings are stored with pgvector on Postgres. On SQLite (used by the test
suite) the same column falls back to JSON and similarity is computed in numpy,
so the rest of the code does not need to care which database it is talking to.
"""
from datetime import UTC, datetime

from sqlalchemy import JSON, Index, UniqueConstraint
from sqlalchemy.types import TypeDecorator
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


class EmbeddingType(TypeDecorator):
    impl = JSON
    cache_ok = True

    def __init__(self, dim=384, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dim = dim

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector(self.dim))
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return [float(x) for x in value]

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return [float(x) for x in value]


class TimestampMixin:
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)


class User(TimestampMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="student")
    study_level = db.Column(db.String(20), nullable=False, default="intermediate")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    last_login_at = db.Column(db.DateTime)

    courses = db.relationship("Course", back_populates="owner", cascade="all, delete-orphan")

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    @property
    def is_admin(self):
        return self.role == "admin"

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name,
            "role": self.role,
            "study_level": self.study_level,
            "created_at": iso(self.created_at),
            "last_login_at": iso(self.last_login_at),
        }


class Course(TimestampMixin, db.Model):
    __tablename__ = "courses"
    __table_args__ = (UniqueConstraint("owner_id", "code", name="uq_course_owner_code"),)

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    code = db.Column(db.String(40), nullable=False)
    description = db.Column(db.Text, default="")
    exam_date = db.Column(db.Date)

    owner = db.relationship("User", back_populates="courses")
    documents = db.relationship("Document", back_populates="course", cascade="all, delete-orphan")

    def to_dict(self, with_counts=False):
        data = {
            "id": self.id,
            "name": self.name,
            "code": self.code,
            "description": self.description or "",
            "exam_date": self.exam_date.isoformat() if self.exam_date else None,
            "created_at": iso(self.created_at),
        }
        if with_counts:
            data["document_count"] = len(self.documents)
            data["ready_documents"] = sum(1 for d in self.documents if d.status == "ready")
        return data


class Document(TimestampMixin, db.Model):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("course_id", "sha256", name="uq_document_course_sha"),)

    STATUSES = ("pending", "processing", "ready", "failed")

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    title = db.Column(db.String(255))
    content_type = db.Column(db.String(120))
    extension = db.Column(db.String(10), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    sha256 = db.Column(db.String(64), nullable=False)
    storage_key = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    error = db.Column(db.Text)
    page_count = db.Column(db.Integer, default=0)
    chunk_count = db.Column(db.Integer, default=0)
    word_count = db.Column(db.Integer, default=0)
    version = db.Column(db.Integer, nullable=False, default=1)
    processing_ms = db.Column(db.Integer)
    processed_at = db.Column(db.DateTime)

    course = db.relationship("Course", back_populates="documents")
    chunks = db.relationship("Chunk", back_populates="document", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "course_id": self.course_id,
            "filename": self.filename,
            "title": self.title or self.filename,
            "extension": self.extension,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "error": self.error,
            "page_count": self.page_count,
            "chunk_count": self.chunk_count,
            "word_count": self.word_count,
            "version": self.version,
            "processing_ms": self.processing_ms,
            "created_at": iso(self.created_at),
            "processed_at": iso(self.processed_at),
        }


class Chunk(TimestampMixin, db.Model):
    __tablename__ = "chunks"
    __table_args__ = (Index("ix_chunks_course_doc", "course_id", "document_id"),)

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    ordinal = db.Column(db.Integer, nullable=False)
    text = db.Column(db.Text, nullable=False)
    token_count = db.Column(db.Integer, nullable=False, default=0)
    page = db.Column(db.Integer)
    section = db.Column(db.String(255))
    embedding = db.Column(EmbeddingType(384))

    document = db.relationship("Document", back_populates="chunks")

    def to_dict(self, include_text=True):
        data = {
            "id": self.id,
            "document_id": self.document_id,
            "ordinal": self.ordinal,
            "page": self.page,
            "section": self.section,
            "token_count": self.token_count,
        }
        if include_text:
            data["text"] = self.text
        return data


class ChatSession(TimestampMixin, db.Model):
    __tablename__ = "chat_sessions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False, default="New session")
    document_filter = db.Column(JSON)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    messages = db.relationship(
        "Message", back_populates="session", cascade="all, delete-orphan", order_by="Message.id"
    )

    def to_dict(self, with_messages=False):
        data = {
            "id": self.id,
            "course_id": self.course_id,
            "title": self.title,
            "document_filter": self.document_filter or [],
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "message_count": len(self.messages),
        }
        if with_messages:
            data["messages"] = [m.to_dict() for m in self.messages]
        return data


class Message(TimestampMixin, db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer, db.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    citations = db.Column(JSON)
    rewritten_query = db.Column(db.Text)
    tokens_in = db.Column(db.Integer, default=0)
    tokens_out = db.Column(db.Integer, default=0)
    latency_ms = db.Column(db.Integer)
    cached = db.Column(db.Boolean, default=False)
    grounded = db.Column(db.Boolean, default=True)
    feedback = db.Column(db.Integer)  # 1 thumbs up, -1 thumbs down

    session = db.relationship("ChatSession", back_populates="messages")

    def to_dict(self):
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "citations": self.citations or [],
            "cached": bool(self.cached),
            "grounded": bool(self.grounded),
            "latency_ms": self.latency_ms,
            "feedback": self.feedback,
            "created_at": iso(self.created_at),
        }


class Summary(TimestampMixin, db.Model):
    __tablename__ = "summaries"
    __table_args__ = (UniqueConstraint("document_id", "style", "doc_version", name="uq_summary_doc_style_ver"),)

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    style = db.Column(db.String(20), nullable=False)
    doc_version = db.Column(db.Integer, nullable=False, default=1)
    content = db.Column(db.Text, nullable=False)
    model = db.Column(db.String(100))

    def to_dict(self):
        return {
            "id": self.id,
            "document_id": self.document_id,
            "style": self.style,
            "content": self.content,
            "model": self.model,
            "created_at": iso(self.created_at),
        }


class StudyGuide(TimestampMixin, db.Model):
    __tablename__ = "study_guides"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    focus_topics = db.Column(JSON)
    plan = db.Column(JSON)
    exam_date = db.Column(db.Date)
    hours_per_day = db.Column(db.Float)

    def to_dict(self):
        return {
            "id": self.id,
            "course_id": self.course_id,
            "title": self.title,
            "content": self.content,
            "focus_topics": self.focus_topics or [],
            "plan": self.plan or [],
            "exam_date": self.exam_date.isoformat() if self.exam_date else None,
            "hours_per_day": self.hours_per_day,
            "created_at": iso(self.created_at),
        }


class Flashcard(TimestampMixin, db.Model):
    __tablename__ = "flashcards"
    __table_args__ = (Index("ix_flashcards_user_due", "user_id", "due_at"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id", ondelete="SET NULL"))
    source_chunk_id = db.Column(db.Integer, db.ForeignKey("chunks.id", ondelete="SET NULL"))
    front = db.Column(db.Text, nullable=False)
    back = db.Column(db.Text, nullable=False)
    topic = db.Column(db.String(120))
    ease = db.Column(db.Float, nullable=False, default=2.5)
    interval_days = db.Column(db.Integer, nullable=False, default=0)
    repetitions = db.Column(db.Integer, nullable=False, default=0)
    lapses = db.Column(db.Integer, nullable=False, default=0)
    due_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_reviewed_at = db.Column(db.DateTime)
    suspended = db.Column(db.Boolean, nullable=False, default=False)

    def to_dict(self):
        return {
            "id": self.id,
            "course_id": self.course_id,
            "document_id": self.document_id,
            "front": self.front,
            "back": self.back,
            "topic": self.topic,
            "ease": round(self.ease, 2),
            "interval_days": self.interval_days,
            "repetitions": self.repetitions,
            "lapses": self.lapses,
            "due_at": iso(self.due_at),
            "last_reviewed_at": iso(self.last_reviewed_at),
            "suspended": self.suspended,
        }


class Quiz(TimestampMixin, db.Model):
    __tablename__ = "quizzes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    difficulty = db.Column(db.String(20), nullable=False, default="medium")
    questions = db.Column(JSON, nullable=False)

    attempts = db.relationship("QuizAttempt", back_populates="quiz", cascade="all, delete-orphan")

    def to_dict(self, reveal_answers=False):
        questions = []
        for q in self.questions or []:
            item = {k: v for k, v in q.items() if reveal_answers or k not in ("answer_index", "explanation")}
            questions.append(item)
        return {
            "id": self.id,
            "course_id": self.course_id,
            "title": self.title,
            "difficulty": self.difficulty,
            "question_count": len(questions),
            "questions": questions,
            "created_at": iso(self.created_at),
        }


class QuizAttempt(TimestampMixin, db.Model):
    __tablename__ = "quiz_attempts"

    id = db.Column(db.Integer, primary_key=True)
    quiz_id = db.Column(db.Integer, db.ForeignKey("quizzes.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    answers = db.Column(JSON, nullable=False)
    score = db.Column(db.Integer, nullable=False)
    total = db.Column(db.Integer, nullable=False)
    duration_s = db.Column(db.Integer)
    breakdown = db.Column(JSON)

    quiz = db.relationship("Quiz", back_populates="attempts")

    def to_dict(self):
        return {
            "id": self.id,
            "quiz_id": self.quiz_id,
            "score": self.score,
            "total": self.total,
            "percent": round(100.0 * self.score / self.total, 1) if self.total else 0.0,
            "duration_s": self.duration_s,
            "breakdown": self.breakdown or [],
            "created_at": iso(self.created_at),
        }


class TopicMastery(db.Model):
    __tablename__ = "topic_mastery"
    __table_args__ = (UniqueConstraint("user_id", "course_id", "topic", name="uq_mastery_user_course_topic"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    topic = db.Column(db.String(120), nullable=False)
    correct = db.Column(db.Integer, nullable=False, default=0)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    @property
    def accuracy(self):
        return self.correct / self.attempts if self.attempts else 0.0

    def to_dict(self):
        return {
            "topic": self.topic,
            "correct": self.correct,
            "attempts": self.attempts,
            "accuracy": round(self.accuracy, 3),
            "updated_at": iso(self.updated_at),
        }


class UsageEvent(TimestampMixin, db.Model):
    __tablename__ = "usage_events"
    __table_args__ = (Index("ix_usage_user_created", "user_id", "created_at"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id", ondelete="SET NULL"))
    kind = db.Column(db.String(30), nullable=False)
    tokens_in = db.Column(db.Integer, default=0)
    tokens_out = db.Column(db.Integer, default=0)
    latency_ms = db.Column(db.Integer)
    cached = db.Column(db.Boolean, default=False)
    model = db.Column(db.String(100))


def iso(value):
    return value.isoformat() + "Z" if value else None
