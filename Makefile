.PHONY: install dev test lint up down logs seed loadtest fmt

install:
	pip install -r requirements-dev.txt

dev:
	APP_ENV=development flask --app wsgi run --debug --port 8000

test:
	APP_ENV=testing pytest -q --cov=app --cov-report=term-missing

lint:
	ruff check app tests

fmt:
	ruff format app tests

up:
	docker compose up --build -d
	@echo "API behind nginx on http://localhost:8080"

down:
	docker compose down

logs:
	docker compose logs -f api worker

seed:
	docker compose exec api python scripts/seed_demo.py

loadtest:
	locust -f loadtest/locustfile.py --headless -u 500 -r 5 -t 8m \
		--host http://localhost:8080 --csv loadtest/results/run --html loadtest/results/report.html
