# Validation performed

Executed on 26 September 2026 using Docker Desktop's Linux engine on Windows, with Python 3.12 application containers and real MySQL, Kafka KRaft, RabbitMQ and Grafana services.

## Results

- **21 tests passed** in the final complete run (`pytest -q`, 20.57 seconds).
- Ruff lint passed; repository Python files were formatted with Ruff.
- Strict mypy passed for the pure domain module.
- Alembic initialized an empty MySQL database and repeated startup completed successfully.
- Seven Grafana dashboards were discovered through the Grafana API. The configured MySQL datasource health check and every dashboard panel SQL query passed.
- The quick simulator completed through the live ingestion API and broker pipeline.
- DT-1047 produced an investigation with approximately **18.6998% accounting difference**, **7.2000% baseline**, **6 of 6 qualifying persistence slots**, and **5 valid meters**. Small numerical differences from the configured 18.7/7.2 reflect six-decimal simulated register rounding.
- At the captured demonstration checkpoint, the quarantine count was **0**.
- `.env` was confirmed excluded by `.gitignore`.

Tests cover register scaling/reset/rollover, invalid numbers, zero/reverse flow, incomplete population, baseline/persistence/scoring, input schema rejection, MySQL duplicate acceptance and late boundary recalculation, independent meter anomaly supersession, RabbitMQ confirm/manual ACK/redelivery, inbox/job-outbox rollback, Grafana queries, and the full streaming investigation workflow with duplicate ingestion and stale case-update rejection.

## Limits of the evidence

This was a local functional/integration run, not a 10,000-meter capacity benchmark or an availability certification. The 90-day full profile, physical-host failures, all broker crash boundaries, sustained rebalance load, external HES interoperability, backup restoration and production security review have not been executed. See deployment.md for those release gates. Grafana queries were verified programmatically; no screenshot-based visual audit is claimed.

The local demonstration stack is left running. Its named volumes retain the demo and uniquely named integration fixtures. `docker compose down` stops services without deleting those volumes.
