# API guide

Both API processes expose the same FastAPI contract. Use port 8000 for metadata, analytics and investigations; port 8001 for reading ingestion. A production proxy should route `/api/v1/readings/*` to ingestion. Interactive OpenAPI is at `/docs`.

The authenticated operator UI is at `http://localhost:8000/login`. It uses its own local user/session store, CSRF-protected forms and role permissions; it does not expose or reuse API keys in the browser. The initial administrator credentials are `UI_ADMIN_USERNAME` and `UI_ADMIN_PASSWORD` in the private local `.env` file. Dashboard, feeder, transformer, meter, investigation, quality, event, topology, simulator and system-health views are server rendered and use bounded backend read models.

## Canonical read API (v2)

Released `/api/v1` response shapes remain unchanged for existing clients. The additive `/api/v2` namespace supplies explicit, versioned Pydantic contracts and a common response envelope:

```json
{"schema_version": 1, "data": {}, "request_id": "uuid"}
```

`GET /api/v2/dashboard` requires `from_time`, `to_time` and accepts repeated `feeder_ids`, `transformer_ids`, `severity`, plus `granularity` (`15m`, `1h`, or `1d`). Ranges are UTC, half-open and limited to 90 days / 3,000 points. It returns the authoritative summary, trend, health, priority entities, quality and activity model in one bounded response.

`GET /api/v2/feeders/{id}` returns feeder identity and hierarchy counts. `.../summary` and `.../trends` require the same `from_time`, `to_time` and optional `granularity`; `.../topology` supports bounded `page` and `page_size`. These endpoints use `SuccessResponse` and concrete Feeder/Dashboard contracts in OpenAPI.

Canonical API errors have a stable envelope with an error code, safe details and request ID. For example, an invalid time range returns `422` with `VALIDATION_ERROR` or `INVALID_REQUEST`; a missing resource returns `404` with `NOT_FOUND`. See [data contracts](data-contracts.md) for field semantics and JSON examples.

Write authentication: `X-API-Key: <API_KEY>`. Read authentication accepts READ_API_KEY or API_KEY; public reads are off by default. Numeric database IDs are distinct from external `entity_code` strings. Time filters require explicit offsets. Decimal quantities serialize as strings.

Create a transformer with `code` and an aligned UTC `valid_from`. Create meters with their `transformer_id`, `multiplier`, `import_only` and `valid_from`. The multiplier defaults to 1. Metadata PUT changes name/active only. Historical assignment changes use POST `/api/v1/meters/{id}/assignments` with `transformer_id` and `valid_from`; this closes the current assignment and rebuilds existing affected aggregates.

POST `/api/v1/readings/meters`, `/transformers` or `/bulk` accepts one reading or an array, up to MAX_BULK_READINGS. Bulk acceptance is atomic. A canonical interval reading:

```json
{
  "event_id": "ed207646-83ea-4aa0-9083-8f0fa1b223ee",
  "entity_code": "M-0000-0000",
  "measurement_kind": "INTERVAL_ENERGY",
  "energy_basis": "PRIMARY",
  "interval_start": "2026-01-01T10:00:00Z",
  "reading_time": "2026-01-01T10:15:00Z",
  "import_energy_kwh": "0.632000",
  "source": "AMI"
}
```

For registers, use CUMULATIVE_REGISTER, omit interval_start and interval-energy fields, and provide import_energy_total_kwh and optional export_energy_total_kwh. The reading_time is a boundary. Same event ID with different data, or conflicting values at the same natural key/revision, returns 409. Explicit correction uses a new event ID and higher revision. A single authoritative source per asset is enforced in MVP.

GET `/api/v1/meters/{id}/energy` returns normalized intervals. `/readings`, `/quality`, `/anomalies` and `/events` provide drilldowns. Transformer `/balance`, `/evaluations`, `/anomalies` and `/meters` provide accounting context. Collections use bounded limits; metadata lists support page/page_size/sort/order. Time-series use `start`, `end`, `limit`, `before_id`; this cursor orders immutable row IDs, while time filters select the event-time range.

Investigations support GET list/detail/history and PATCH with version, status, assigned_to and resolution. Creation is automatic from persistent findings. Version conflicts return 409. Resolution/dismissal requires a reason. The server-rendered page `/investigations/{id}` uses the same workflow. User decisions and original evidence remain in the audit trail after recalculations.

POST `/api/v1/events` accepts typed meter events. POST `/api/v1/reprocessing-jobs` accepts transformer_id, start_time and end_time, at most 31 days. GET the returned job ID to inspect dispatch state.

The running OpenAPI contract is the authoritative list of implemented endpoints. Manual investigation creation, arbitrary anomaly status editing, report generation and notification delivery are not exposed in this MVP.
