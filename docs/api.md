# API guide

Both API processes expose the same FastAPI contract. Use port 8000 for metadata, analytics and investigations; port 8001 for reading ingestion. A production proxy should route `/api/v1/readings/*` to ingestion. Interactive OpenAPI is at `/docs`.

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
