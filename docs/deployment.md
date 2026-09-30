# Deployment

## On AWS

```
Route 53 -> ALB (HTTPS) -> ECS Fargate: api service (2 to 6 tasks, CPU target tracking)
                           ECS Fargate: worker service (Celery, ingest queue)
                           RDS PostgreSQL 16 with pgvector (encrypted, 7 day backups)
                           ElastiCache Redis 7 (primary plus replica, automatic failover)
                           S3 bucket for uploads (private, encrypted)
                           Secrets Manager for SECRET_KEY and DATABASE_URL
                           CloudWatch log groups and a 5xx alarm on the ALB
```

Everything is in `deploy/terraform`. Tasks run in private subnets and only the ALB is public. The data security group only accepts Postgres and Redis traffic from the app security group.

### First deploy

```bash
cd deploy/terraform
terraform init
terraform apply -var certificate_arn=arn:aws:acm:us-west-2:...:certificate/...
```

This creates the network, database, cache, bucket, repository and cluster. The services will not become healthy until an image exists, which the deploy script handles:

```bash
./deploy/deploy.sh v1.0.0
```

The script logs in to ECR, builds and pushes the image, applies Terraform with the new tag, runs `flask init-db` as a one off task (creates the pgvector extension, tables and the HNSW and full text indexes), and waits for both services to be stable. ECS deployment circuit breakers roll back automatically if new tasks keep failing their health checks.

Create an admin account from a one off task the same way:

```bash
aws ecs run-task --cluster studymate --task-definition studymate-api --launch-type FARGATE \
  --overrides '{"containerOverrides":[{"name":"api","command":["flask","--app","wsgi","create-admin","--email","you@uc.edu","--password","..."]}]}' \
  --network-configuration "awsvpcConfiguration={subnets=[...],securityGroups=[...]}"
```

### Model providers on AWS

`llm_provider` defaults to `bedrock`. The task role already allows `bedrock:InvokeModel` and `InvokeModelWithResponseStream`. Model access has to be enabled once in the Bedrock console for the account and region. To use OpenAI instead, add `OPENAI_API_KEY` to the secret and set `llm_provider = "openai"`.

If you switch embeddings to OpenAI, keep `EMBEDDING_DIM=384`. The provider passes the `dimensions` parameter so vectors still fit the existing column. Changing the dimension means a migration and a full reindex.

### Sizing

The defaults (API tasks at 1 vCPU and 2 GB, a `db.t4g.medium`, a `cache.t4g.small`) are what I would start a class sized deployment on. The load test in `docs/load-test-report.md` ran 500 simulated students on a single 2 vCPU machine that also hosted Postgres, Redis and the load generator, so two Fargate tasks have a lot of headroom for the platform itself. With a real model the limit becomes the provider's rate limit and latency, not this service.

Watch these metrics first:

- `studymate_http_request_seconds` p95 by route
- `studymate_llm_seconds` and `studymate_llm_calls_total{outcome="error"}`
- `studymate_cache_events_total` hit ratio for the answer cache
- `studymate_rate_limited_total` by scope, to see whether the limits are too tight for real use
- RDS connections: each API task opens at most `workers * (DB_POOL_SIZE + DB_MAX_OVERFLOW)` connections

## With Docker Compose

`docker compose up --build` runs the API, a Celery worker, Celery beat, Postgres with pgvector, Redis and nginx on port 8080. The API container runs `init-db` on start. This is the setup I use for local development and demos.

## Configuration

All settings come from environment variables; `.env.example` lists the common ones and `app/config.py` has the rest with their defaults. In production the app refuses to start if `SECRET_KEY` is missing or short, if S3 storage is selected without a bucket, or if OpenAI is selected without a key.

## Gunicorn notes

- `gthread` workers with 16 threads, because almost all request time is spent waiting on Postgres, Redis or the model API. A streaming answer holds one thread for its whole duration.
- Keep-alive is 75 seconds so it outlasts the ALB idle timeout of 60 seconds. With the default of a few seconds, the server sometimes closed an idle connection at the moment the client reused it, which showed up as connection resets in the load test.
- Workers are recycled after about 50,000 requests. At 2,000 the recycles were frequent enough under load to drop keep-alive connections visibly.
- `PROMETHEUS_MULTIPROC_DIR` is set in the image so `/metrics` aggregates all workers.
