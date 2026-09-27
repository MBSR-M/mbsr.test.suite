# Contributing

Use Python 3.12 or the Docker image. Install with `python -m pip install -r requirements.lock` and `python -m pip install -e . --no-deps`. Run `ruff check .`, `ruff format --check .`, `mypy`, and pytest. Infrastructure tests require the Compose stack and must use MySQL rather than SQLite.

Keep domain calculations pure and use Decimal. Include regression coverage for measurement semantics, idempotency, correction propagation or concurrency behavior affected by your changes. Add a new Alembic revision for schema changes; do not edit frozen released migrations. Regenerate and review `database/schema.sql` when schema changes.

Use observation, accounting difference, anomaly and investigation language. Scores are never allegations or probabilities of wrongdoing. Do not commit `.env`, private meter identifiers, collector credentials or production datasets.
