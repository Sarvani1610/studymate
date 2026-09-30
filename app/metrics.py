"""Prometheus metrics.

When gunicorn runs several workers set PROMETHEUS_MULTIPROC_DIR so each worker
writes to a shared directory; the /metrics endpoint then aggregates them.
"""
import os

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

REQUESTS = Counter("studymate_http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram(
    "studymate_http_request_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16),
)
LLM_CALLS = Counter("studymate_llm_calls_total", "LLM calls", ["provider", "task", "outcome"])
LLM_TOKENS = Counter("studymate_llm_tokens_total", "LLM tokens", ["provider", "direction"])
LLM_LATENCY = Histogram(
    "studymate_llm_seconds", "LLM latency", ["provider", "task"], buckets=(0.1, 0.5, 1, 2, 4, 8, 16, 32)
)
CACHE_EVENTS = Counter("studymate_cache_events_total", "Cache hits and misses", ["cache", "result"])
RATE_LIMITED = Counter("studymate_rate_limited_total", "Requests rejected by the rate limiter", ["scope"])
INGEST_JOBS = Counter("studymate_ingest_jobs_total", "Document ingestion jobs", ["outcome"])
INGEST_SECONDS = Histogram(
    "studymate_ingest_seconds", "Document ingestion time", buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300)
)
RETRIEVAL_SECONDS = Histogram(
    "studymate_retrieval_seconds", "Hybrid retrieval time", buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1)
)
ACTIVE_STREAMS = Gauge("studymate_active_streams", "Open SSE chat streams", multiprocess_mode="livesum")


def observe_request(method, route, status, seconds):
    REQUESTS.labels(method, route, str(status)).inc()
    LATENCY.labels(method, route).observe(seconds)


def render_metrics():
    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST
