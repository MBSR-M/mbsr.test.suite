# Database implementation

Alembic migration 0001 creates the schema from frozen `src/opengrid/schema_v1.py`. `database/schema.sql` is an export of that same metadata. Application mappings are in `src/opengrid/db.py`; subsequent changes require new migrations. All DATETIME(6) values represent UTC, with aware validation at the API boundary and explicit UTC serialization.

The MVP unifies meters and transformers in `asset` with a kind discriminator. Assignments and configurations are effective dated and refer to that table with FKs. Readings are immutable revisions keyed by asset/time/source/revision plus unique event UUID. One authoritative source per asset is enforced by the service under an asset row lock.

Interval, aggregate, balance and evaluation tables contain current query projections. Changed interval/balance/anomaly evidence is appended to `projection_revision` before updating the current row. Raw reading IDs and configuration IDs in interval evidence preserve calculation lineage. The production design proposes more fully normalized revision tables and immutable baseline snapshots; the MVP stores those summaries in evidence JSON.

Inbox insertion, domain mutation and outbox insertion commit in one transaction. Session-per-transaction and READ COMMITTED isolation avoid stale snapshots after lock waits. All workers serialize changes by asset using row locks. Outbox publishers use SKIP LOCKED and a bounded confirm transaction; see implementation-status.md for the lease-relay optimization still planned.

Grafana reads only seven curated views through its dedicated database user. MySQL tables use foreign keys and unique constraints and remain unpartitioned. No Kafka position is stored as business data; failed transport coordinates are diagnostic quarantine metadata.

Generate schema: `docker compose run --rm --no-deps -v "$PWD:/app" test python scripts/export_schema.py`. Review generated DDL with migration changes. Never run production migrations without a backup and a tested rollback/recovery strategy.
