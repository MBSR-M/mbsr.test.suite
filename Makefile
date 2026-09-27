.PHONY: install up down logs test lint format migrate seed simulate clean
install:
	python -m pip install -e '.[dev]'
up:
	docker compose up -d --build --wait --wait-timeout 180
down:
	docker compose down
logs:
	docker compose logs -f --tail=100
test:
	docker compose run --rm test
lint:
	docker compose run --rm test ruff check .
	docker compose run --rm test mypy
format:
	python -m ruff format .
migrate:
	docker compose run --rm migrate
seed: simulate
simulate:
	docker compose run --rm simulator
clean:
	@echo 'Use docker compose down to stop. Data volumes are intentionally retained.'
