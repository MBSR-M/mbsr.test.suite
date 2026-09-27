# Deployment and operations

The checked-in Compose deployment is a local single-host MVP. It binds API 8000, ingestion 8001 and Grafana 3000 to `127.0.0.1`. Database and broker ports are internal. The development override exposes MySQL and RabbitMQ management to loopback only.

## Start

Linux: `sh scripts/bootstrap.sh`. Windows PowerShell 7: `./scripts/setup.ps1`, then `docker compose up -d --build`. Setup creates random local credentials only if `.env` is absent. Do not commit it. Passwords must be URL-safe because DATABASE_URL embeds the application password; generated hex values satisfy this.

Compose builds one image and uses six entry points. MySQL readiness precedes Alembic; Kafka volume ownership is initialized before its non-root broker starts; topic, queue and read-only reporting-user initialization precedes applications. Initialization is repeatable. Migrations are not run independently by each API process.

Grafana username is `admin`; its password is `GRAFANA_PASSWORD` in your local `.env`. `/docs` on ports 8000/8001 describes the APIs. Supply `API_KEY` in `X-API-Key` for writes or `READ_API_KEY` for reads. Do not paste keys into dashboard URLs. For case actions, use the dashboard's case link and enter your operator key in the local form.

## Test and simulate

```sh
docker compose run --rm test
docker compose run --rm --no-deps test pytest -q
docker compose run --rm simulator
docker compose run --rm simulator python -m opengrid.simulator --profile full --days 90
```

The quick simulator uses one transformer and five meters, 30 days of evening-slot history, then a six-hour imbalance. Gaps between evening windows are deliberately missing and never interpolated. Full mode uses 100 transformers, 10,000 meters, every 15-minute boundary and 90 days by default. It generates tens of millions of observations; capacity-test and provision storage before that run. Full-scale throughput has not been certified.

Use a fresh demo database or distinct scenario/source codes when changing simulation scenarios. Replaying an identical seeded run is idempotent; contradictory observations at the same natural key correctly conflict.

## Operate

`docker compose logs -f --tail=100` shows JSON application logs. `/api/v1/system`, `/metrics` and Grafana show worker status and accounting data. `docker compose down` preserves data; deleting volumes destroys the local dataset. A worker stopping during an event leaves either no domain transaction or a completed inbox record, so restarting safely resumes Kafka consumption.

The reprocessing API accepts bounded transformer time ranges. RabbitMQ dispatches durable recomputation events. Job status DISPATCHED means commands were emitted; it does not claim the recalculations have all completed. Inspect interval/balance/evaluation results to confirm downstream completion. A production job-progress tracker is a remaining hardening item.

Permanent processing errors are quarantined with transport coordinates; infrastructure database failures restart the worker without advancing its Kafka position. Inspect the error, correct the source/code, and perform controlled replay. There is no public unauthenticated quarantine replay endpoint.

## Production release gates

Before using live customer data, implement and validate these design requirements:

- TLS reverse proxy and per-operator identity/scoped credentials; separate secrets and DB privileges per service, with root credentials only in initialization.
- Kafka and RabbitMQ authentication/ACLs and isolated host networking. Current internal Kafka is plaintext for local Compose.
- Storage sizing, controlled retention/archiving, load testing of 10,000-meter bursts and full history, and backfill throttling.
- Off-host backups, binary-log recovery, an actual restore rehearsal and agreed RPO/RTO.
- Bounded chunked reprocessing with completion tracking; fault tests covering host/broker failure and long rebalances.
- Dependency vulnerability review, image digest pinning and operational alert routing.

Single-host Compose cannot provide host-failure high availability. No Kubernetes, cloud subscription, paid API or commercial Grafana feature is required.
