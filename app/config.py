"""Application configuration.

Everything is driven by environment variables so the same image runs locally,
in CI and on AWS. Defaults are tuned for local development with docker compose.
"""
import os


def _bool(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    raw = os.getenv(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def _float(name, default):
    raw = os.getenv(name)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        return default


class Config:
    APP_NAME = "StudyMate"
    ENV_NAME = "base"

    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me-before-deploying-anywhere")
    JWT_ALGORITHM = "HS256"
    JWT_ACCESS_TTL_MIN = _int("JWT_ACCESS_TTL_MIN", 30)
    JWT_REFRESH_TTL_DAYS = _int("JWT_REFRESH_TTL_DAYS", 14)

    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://studymate:studymate@localhost:5432/studymate",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_size": _int("DB_POOL_SIZE", 10),
        "max_overflow": _int("DB_MAX_OVERFLOW", 20),
        "pool_recycle": 1800,
    }

    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    REDIS_FAKE = False
    CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", os.getenv("REDIS_URL", "redis://localhost:6379/1"))
    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", os.getenv("REDIS_URL", "redis://localhost:6379/2"))
    INGEST_ASYNC = _bool("INGEST_ASYNC", True)

    # File storage
    STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local")  # local or s3
    UPLOAD_DIR = os.getenv("UPLOAD_DIR", os.path.join(os.getcwd(), "var", "uploads"))
    S3_BUCKET = os.getenv("S3_BUCKET", "")
    S3_PREFIX = os.getenv("S3_PREFIX", "uploads/")
    AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
    MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 25)
    ALLOWED_EXTENSIONS = {"pdf", "docx", "txt", "md", "pptx"}

    # Models
    EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "local")  # local, openai
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    EMBEDDING_DIM = _int("EMBEDDING_DIM", 384)
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "stub")  # stub, openai, bedrock
    LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
    LLM_MAX_TOKENS = _int("LLM_MAX_TOKENS", 800)
    LLM_TEMPERATURE = _float("LLM_TEMPERATURE", 0.2)
    LLM_TIMEOUT_S = _int("LLM_TIMEOUT_S", 45)
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")

    # Chunking and retrieval
    CHUNK_SIZE_TOKENS = _int("CHUNK_SIZE_TOKENS", 220)
    CHUNK_OVERLAP_SENTENCES = _int("CHUNK_OVERLAP_SENTENCES", 1)
    RETRIEVAL_TOP_K = _int("RETRIEVAL_TOP_K", 6)
    RETRIEVAL_CANDIDATES = _int("RETRIEVAL_CANDIDATES", 30)
    RETRIEVAL_MIN_SCORE = _float("RETRIEVAL_MIN_SCORE", 0.05)
    MMR_LAMBDA = _float("MMR_LAMBDA", 0.7)
    CONTEXT_TOKEN_BUDGET = _int("CONTEXT_TOKEN_BUDGET", 2500)
    HISTORY_TURNS = _int("HISTORY_TURNS", 6)

    # Caching
    CACHE_TTL_ANSWER = _int("CACHE_TTL_ANSWER", 3600)
    CACHE_TTL_EMBEDDING = _int("CACHE_TTL_EMBEDDING", 86400)
    CACHE_TTL_SUMMARY = _int("CACHE_TTL_SUMMARY", 86400 * 7)
    CACHE_TTL_STATS = _int("CACHE_TTL_STATS", 60)

    # Rate limiting and quotas
    RATELIMIT_ENABLED = _bool("RATELIMIT_ENABLED", True)
    RATE_LIMIT_DEFAULT = os.getenv("RATE_LIMIT_DEFAULT", "120/minute")
    RATE_LIMIT_LLM = os.getenv("RATE_LIMIT_LLM", "20/minute")
    RATE_LIMIT_AUTH = os.getenv("RATE_LIMIT_AUTH", "10/minute")
    RATE_LIMIT_UPLOAD = os.getenv("RATE_LIMIT_UPLOAD", "30/hour")
    DAILY_TOKEN_BUDGET = _int("DAILY_TOKEN_BUDGET", 200000)

    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_JSON = _bool("LOG_JSON", True)
    SLOW_REQUEST_MS = _int("SLOW_REQUEST_MS", 1500)
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")


class DevelopmentConfig(Config):
    ENV_NAME = "development"
    LOG_JSON = _bool("LOG_JSON", False)


class TestingConfig(Config):
    ENV_NAME = "testing"
    TESTING = True
    SECRET_KEY = "test-secret-key-that-is-long-enough-for-hs256"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    REDIS_FAKE = True
    INGEST_ASYNC = False
    STORAGE_BACKEND = "local"
    EMBEDDING_PROVIDER = "local"
    LLM_PROVIDER = "stub"
    LOG_JSON = False
    LOG_LEVEL = "WARNING"
    RATE_LIMIT_DEFAULT = "1000/minute"
    RATE_LIMIT_LLM = "1000/minute"
    RATE_LIMIT_AUTH = "1000/minute"
    RATE_LIMIT_UPLOAD = "1000/minute"


class ProductionConfig(Config):
    ENV_NAME = "production"

    @classmethod
    def validate(cls):
        problems = []
        if cls.SECRET_KEY.startswith("dev-") or len(cls.SECRET_KEY) < 32:
            problems.append("SECRET_KEY must be set to a random value of at least 32 characters")
        if cls.STORAGE_BACKEND == "s3" and not cls.S3_BUCKET:
            problems.append("S3_BUCKET is required when STORAGE_BACKEND=s3")
        if cls.LLM_PROVIDER == "openai" and not cls.OPENAI_API_KEY:
            problems.append("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        return problems


CONFIGS = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def get_config(name=None):
    name = name or os.getenv("APP_ENV", "development")
    return CONFIGS.get(name, DevelopmentConfig)
