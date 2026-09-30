"""Gunicorn settings.

gthread workers because most request time is spent waiting on Postgres, Redis
or the LLM API, not on the CPU. Streaming answers hold a thread for the whole
answer, so threads are set higher than the usual 2 to 4.
"""
import multiprocessing
import os
import shutil

bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")
workers = int(os.getenv("GUNICORN_WORKERS", max(2, multiprocessing.cpu_count())))
worker_class = "gthread"
threads = int(os.getenv("GUNICORN_THREADS", 16))
timeout = int(os.getenv("GUNICORN_TIMEOUT", 120))
graceful_timeout = 30
# Keep-alive has to outlast the load balancer's idle timeout (60s on the ALB), otherwise
# the server closes an idle connection at the same moment the client reuses it and the
# client sees a connection reset. This was the source of the leftover resets in testing.
keepalive = int(os.getenv("GUNICORN_KEEPALIVE", 75))
# Recycling workers guards against slow memory growth, but every recycle drops the
# worker's keep-alive connections. At 2000 this caused visible connection resets in
# the 500 user load test, so it is now high enough to happen only a few times a day.
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", 50000))
max_requests_jitter = 5000
accesslog = None  # the app writes its own structured access log
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info").lower()
forwarded_allow_ips = "*"


def on_starting(server):
    prom_dir = os.getenv("PROMETHEUS_MULTIPROC_DIR")
    if prom_dir:
        shutil.rmtree(prom_dir, ignore_errors=True)
        os.makedirs(prom_dir, exist_ok=True)


def child_exit(server, worker):
    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)
