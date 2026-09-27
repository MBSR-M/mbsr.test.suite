# OpenGrid data contracts

Canonical contracts are executable Pydantic models in `src/opengrid/contracts/`. They validate the additive read API and generate its OpenAPI response schemas. Existing request types remain in `contracts/legacy.py` and are re-exported at their original import paths, so the released ingestion, API-key and worker contracts retain their original behavior.

Canonical contracts use `schema_version: 1`. The additive `/api/v2` namespace distinguishes canonical response shapes from the released `/api/v1` arrays; the HTTP API version and payload schema version are separate identifiers. Consumers reject unknown schema versions and unsupported calculation states. A breaking field meaning, requiredness or enum change requires a new schema version and coordinated producer/consumer migration. Never reinterpret a released field in place.

## Ownership and field reference

| Contract family | Owner | Meaning |
|---|---|---|
| Meter, transformer, raw registers | Ingestion/domain | Measured device values and stable entity identity |
| Feeder, topology | Topology/domain | Explicit feeder → transformer → meter relationships |
| Interval energy, register delta, balance | Accounting domain | Successful derivation or an explicit unavailable state |
| Data quality | Quality domain | Expected/received observations and quality state |
| Anomaly, evidence, score components | Anomaly domain | Observations, evidence and investigation score |
| Investigation, notes, audit | Investigation domain | Versioned human workflow and append-only activity |
| Feeder summary/trend, dashboard | Read-model service | Authoritative period-bound analytics, not frontend calculations |
| Success, pagination, errors, envelope | Boundary layer | Stable transport structure and correlation |

The complete field-by-field machine-readable reference is in [generated JSON schemas](../examples/contracts/schemas/). Each schema specifies the exact property type, required field list, null alternatives, enums, defaults, bounds and nested types. Matching [validated JSON examples](../examples/contracts/manifest.json) are generated from the same model instances, rather than maintained independently. Examples illustrate semantics; they do not assert the live demo has those counts or scores.

Common field rules:

| Field(s) | Type/unit | Requiredness and semantics |
|---|---|---|
| `schema_version` | Integer literal `1` | Default 1; string `"1"`, booleans and version 2 are rejected |
| `*_id` entity identifiers | Positive integer | Existing database IDs are preserved; they are not changed to UUIDs |
| `event_id`, `correlation_id`, `request_id` | UUID | Required on canonical transport envelopes; legacy correlation can be unknown |
| `meter_no`, `transformer_code`, `feeder_code` | String, 1–64 characters | Stable operational codes, separate from internal IDs |
| `*_count`, interval counts | Strict non-negative integer | Count of the specified population/period; nullable only where the counter is unavailable |
| `reading_time` | UTC timestamp | Device observation time |
| `received_at` | UTC timestamp | Ingress/HES reception time |
| `processed_at` | Nullable UTC timestamp | Successful processing time; null before processing |
| `interval_start`, `interval_end` | UTC timestamps | Exact aligned half-open `[start,end)` 15-minute interval |
| `period_start`, `period_end` | UTC timestamps | Read-model aggregation period, inclusive start/exclusive end |
| `energy_import_total_kwh`, `energy_export_total_kwh` | Required Decimal, kWh | Cumulative registers; both present and non-negative on canonical raw readings |
| `import_energy_kwh`, `export_energy_kwh` | Required Decimal, kWh | Successfully derived interval quantities; not cumulative registers or power |
| `import_power_kw`, `export_power_kw`, `avg_*_power_kw` | Nullable Decimal, kW | Power; unavailable measurements remain null |
| `voltage_avg_v`, `current_avg_a`, `frequency_avg_hz` | Nullable Decimal, V/A/Hz | Optional physical measurements |
| Analytical `input_energy_kwh`, `downstream_energy_kwh` | Nullable signed Decimal, kWh | Net import minus export over the declared period |
| `accounting_difference_kwh` | Nullable signed Decimal, kWh | Net input minus valid comparable downstream energy |
| `accounting_difference_percent` | Nullable signed Decimal, % | Difference/input×100 where valid; null for missing or nonpositive input |
| `data_completeness_percent`, `confidence_score`, `anomaly_score` | Decimal in 0–100 | Separate completeness, evidence sufficiency and anomaly score; no probability claim |
| `deviation_percentage_points` | Nullable signed Decimal, percentage points | Current imbalance minus comparable historical baseline |
| `components` | Nullable fractions in 0–1 | Only actual engine components; missing components are not invented |

All canonical models forbid unknown properties. Integers reject coercion from strings and booleans. Decimal values must be finite; NaN, Infinity and malformed values fail validation. JSON encodes Decimals as strings to preserve exact precision. Input decimal strings and numeric JSON values are accepted, while all business calculations use Decimal. Browser number conversion is allowed only when drawing a chart, not to calculate business metrics.

## Time, precision and nullability

All wire timestamps require an explicit UTC offset and normalize to ISO-8601 `Z`. Naive datetimes and numeric epoch values are rejected. The persistence layer uses UTC `DATETIME(6)`; explicit DTO adapters attach UTC when reading its known-naive representation. Missing creation or detection timestamps in the legacy schema remain null with an explanation; current time is never substituted for an unknown historical timestamp.

The MVP uses quarter-hour boundaries and exact 15-minute intervals. The register at 10:00 is the start boundary, and the register at 10:15 is the end boundary for `[10:00,10:15)`. Domain ingestion preserves this boundary interpretation. The dashboard and feeder read models are bounded to 90 days; filters can lower that limit through validation context. Granularity must keep trends to at most 3,000 points. Full meter/event lists and topology remain separate bounded APIs.

Persisted import/export energy uses `DECIMAL(20,6)`. Canonical raw energy matches this precision; excess fractional digits are rejected instead of silently rounded. Aggregate signed energy retains up to six fractional digits but allows larger total magnitudes than a single source row. Derived statistical baselines and percentages retain their full Decimal precision, including repeating means. Rounding in the UI changes display only.

| State | Contract representation | Operator meaning |
|---|---|---|
| Measured/derived zero | `"0"` and a known source | Actual zero, displayed as 0 kWh |
| Unavailable | `null`, source `UNAVAILABLE` | No valid value; display N/A or Missing |
| Partial | Numeric subtotal, availability `PARTIAL` | Incomplete coverage; label it and provide counts |
| Invalid | Explicit quality/calculation status | Source exists but fails validation |
| Estimated | `ESTIMATED` source and documented method | Deliberate estimate, not a measured observation |

`OptionalEnergyContract` requires null exactly when source is UNAVAILABLE. `EnergyMetricContract` couples value/source/availability to expected and valid interval counts. AVAILABLE requires full non-empty coverage, PARTIAL requires some but not all coverage, and UNAVAILABLE requires null. No adapter may use `value or 0` to replace absent energy.

Canonical raw readings require both cumulative registers. Sources that omit export must either be rejected or pass an explicitly invoked `import_only_register_adapter(..., policy_source=...)` after import-only configuration is confirmed. It records `EXPLICIT_IMPORT_ONLY_POLICY` and its source; model validation never invents zero export. The released legacy request schema keeps its optional export field because changing it would break current integrations.

`MeterIntervalEnergyContract` represents successful derivation only. Energy, deltas and multiplier are non-null, energy must equal delta×multiplier, and optional average power must equal energy÷0.25 h. Reset, replacement and invalid calculations use `UnavailableIntervalEnergyContract`; they cannot emit zero consumption. Rollover requires a recovery policy; estimates require an estimated source and a documented method. `RegisterDeltaContract` preserves the signed raw delta and rejects a negative NORMAL delta.

Transformer balance separates raw measurement availability from accounting eligibility. Missing operands force null accounting difference and percentage. Partial downstream population cannot be presented as a complete accounting difference. A zero input and zero difference still have a null percentage. Signed net input is supported for reverse flow; the existing domain's nonpositive-input behavior remains authoritative. The canonical adapter may expose a partial downstream subtotal, but it does not present that subtotal as full network coverage.

## Meter, transformer, energy and workflow contracts

| Contract | Generated JSON example |
|---|---|
| MeterContract | [meter](../examples/contracts/meter.json) |
| TransformerContract | [transformer](../examples/contracts/transformer.json) |
| MeterReadingContract | [meter reading](../examples/contracts/meter-reading.json) |
| TransformerReadingContract | [transformer reading](../examples/contracts/transformer-reading.json) |
| RegisterDeltaContract | [register delta](../examples/contracts/register-delta.json) |
| MeterIntervalEnergyContract | [valid interval](../examples/contracts/meter-interval-energy.json) |
| UnavailableIntervalEnergyContract | [missing interval](../examples/contracts/unavailable-interval-energy.json) |
| OptionalEnergyContract | [unavailable optional energy](../examples/contracts/optional-energy.json) |
| EnergyMetricContract | [partial coverage](../examples/contracts/energy-metric.json) |
| TransformerEnergyBalanceContract | [transformer balance](../examples/contracts/transformer-balance.json) |
| DataQualityContract | [quality](../examples/contracts/data-quality.json) |
| AnomalyContract | [anomaly](../examples/contracts/anomaly.json) |
| AnomalyEvidenceContract | [structured evidence](../examples/contracts/anomaly-evidence.json) |
| ScoreComponentsContract | [components](../examples/contracts/score-components.json) |
| InvestigationContract | [investigation](../examples/contracts/investigation.json) |
| InvestigationNoteContract | [note](../examples/contracts/investigation-note.json) |
| AuditEventContract | [audit](../examples/contracts/audit-event.json) |
| EventContract | [source event](../examples/contracts/event.json) |

Anomaly IDs and investigation IDs remain positive integers because that is the established repository identity. `ENERGY_IMBALANCE` in legacy storage maps explicitly to canonical `TRANSFORMER_IMBALANCE`; it is not silently rewritten in historical rows. Canonical anomalies include SUPERSEDED for corrected observations. Evidence fields that the engine never captured remain null. `detected_at` is unknown for old rows; interval time is not mislabeled as processing time.

Investigation statuses are NEW, ASSIGNED, INVESTIGATING, RESOLVED and DISMISSED. Closed cases require a resolution and closure time; reopened cases have no closure time. Existing optimistic version checks remain in the workflow service. Notes are append-only. Audit payloads preserve actor, action, entity ID, time and old/new values; no secret or credential fields belong in them.

## Feeder Contract

[FeederContract example](../examples/contracts/feeder.json) separates `feeder_id` from `feeder_code`, name, substation and active state. Transformer/meter counts represent the hierarchy. Legacy storage lacks creation/update timestamps and substation, so they remain null with `metadata_unavailable_reason` instead of fabricated metadata.

## Feeder Summary Contract

[FeederSummaryContract example](../examples/contracts/feeder-summary.json) is period-bound analytics, distinct from the entity. It includes explicit energy source, completeness/confidence, anomaly and open-case counts, and backend-derived health. This MVP has no physical upstream feeder meter; sums of transformer net input are labeled DERIVED, never MEASURED. No measurements yields UNAVAILABLE. UNKNOWN health is an explicit extension for unmeasured entities, so they are not mislabeled healthy.

## Feeder Trend Contract

[FeederTrendContract example](../examples/contracts/feeder-trend.json) includes period, feeder ID, aggregation interval and bounded [trend points](../examples/contracts/feeder-trend-point.json). Missing points retain null energy and an availability reason. The browser receives finished aggregate values.

## Feeder Topology Contract

[FeederTopologyContract example](../examples/contracts/feeder-topology.json) nests bounded [transformer nodes](../examples/contracts/transformer-topology.json) and [meter nodes](../examples/contracts/meter-topology.json). Its page/page_size/total/pages describe transformer pagination. A meter preview sets `meters_truncated` when additional meters exist; it never pretends a preview is the full population.

## Dashboard Filter Contract

[DashboardFilterContract example](../examples/contracts/dashboard-filter.json) specifies from_time, to_time, feeder IDs, transformer IDs, severities and 15m/1h/1d granularity. IDs and severities are bounded/deduplicated; invalid ranges and excessive point counts fail before querying.

## Dashboard Summary Contract

[DashboardSummaryContract example](../examples/contracts/dashboard-summary.json) provides the selected period, population counts, energy quantities and sources, completeness/confidence, open-case and anomaly counts and transformer health counts. Null energy is preserved. UNKNOWN transformers have their own count. Backend aggregation owns every quantity.

## Dashboard Trend Contract

[DashboardTrendContract example](../examples/contracts/dashboard-trend.json) contains [typed points](../examples/contracts/dashboard-trend-point.json) in unique ascending time order, inside the declared half-open period. Missing and partial buckets remain distinguishable from measured zeros.

## Dashboard Health Contract

[DashboardHealthDistributionContract example](../examples/contracts/dashboard-health.json) provides healthy/watch/high/critical/unknown counts. These are derived by the read service from current evidence, never calculated in JavaScript.

## Dashboard Transformer Contract

[DashboardTransformerItemContract example](../examples/contracts/dashboard-transformer.json) carries transformer/feeder IDs and codes, current and baseline imbalance, deviation in percentage points, accounting difference, persistence, completeness, confidence, score, severity and open-case count. Unknown evidence remains null.

## Dashboard Meter Contract

[DashboardMeterItemContract example](../examples/contracts/dashboard-meter.json) contains meter/transformer identity, controlled anomaly type, current/baseline consumption, deviation, completeness, score/severity and last reading time. Statistical baseline precision is retained independently of persisted energy precision.

## Dashboard Investigation Contract

[DashboardInvestigationItemContract example](../examples/contracts/dashboard-investigation.json) contains numeric case/entity IDs, display name, priority and severity, optional unaccounted estimate, status, assignee and opening time. It does not replace the full workflow contract.

## Dashboard Data Quality Contract

[DashboardDataQualityContract example](../examples/contracts/dashboard-data-quality.json) gives the period and expected/received/missing/duplicate/late/invalid counts plus affected assets. Counters that legacy persistence does not measure are null, with a reason; duplicate retries are not manufactured as zero.

## Dashboard Activity Contract

[DashboardActivityContract example](../examples/contracts/dashboard-activity.json) identifies the source-qualified activity, event time, controlled category, entity, title, explanation and severity. Source-qualified IDs such as `anomaly:82` preserve existing row identity without inventing UUID history.

## Complete Dashboard Contract

[DashboardContract example](../examples/contracts/dashboard.json) combines generated_at, filters, summary, trend, health, bounded top entities, data quality and recent activity. Every analytical section must use the same requested period. It supplies the authoritative operator read model in one request; full lists are separate paginated resources.

## Transport, compatibility and validation

Canonical single-entity responses use [SuccessResponse](../examples/contracts/api-success.json); collections use [PaginatedResponse](../examples/contracts/api-pagination.json) with request IDs and consistent counts. Empty collections have `items: []`, `total: 0`, `pages: 0`. [ErrorResponse](../examples/contracts/api-error.json) provides code, safe message/details and request ID. Legacy routes retain their released error/array shape; the new version documents the uniform envelope explicitly.

[KafkaEnvelopeContract](../examples/contracts/kafka-envelope.json) carries event_id/type, schema_version, UTC event_time, producer, correlation_id and a typed payload. The central event registry validates each canonical event against its payload model. Unknown event kinds/versions fail closed. Invalid worker messages use the existing quarantine/dead-letter policy before domain mutation.

The current reliable pipeline publishes references to persisted observations and projections. `LegacyEventEnvelopeContract` describes that released reference format separately. A `reading_id` reference is not a complete canonical register measurement, so canonical validation must not reinterpret it as one. Correlation is explicit on new envelopes; missing old correlation remains unknown. Migration of pipeline payload shapes requires a new topic/version or a documented dual-reader rollout; no silent bulk rewrite is performed.

DTO mappings are explicit: naive database timestamps → aware UTC; numeric ORM IDs → numeric contract IDs; Decimal columns → Decimal fields; nullable derived operands → nullable contract values; legacy codes/types → controlled canonical names. Read models use bounded projection queries, not unbounded raw reading scans. Settings and error DTOs never expose database URLs, passwords, API keys or environment contents.

To update a contract: change the model, version breaking semantics, update adapters/producers/consumers, regenerate fixtures/schemas, update this guide, run compatibility tests and review OpenAPI. Regeneration is deterministic:

```sh
python -m opengrid.contracts.generate --output examples/contracts
pytest -q tests/test_data_contracts.py -m 'not integration'
pytest -q tests/test_data_contracts.py -m integration
```

The suite validates every generated example and schema; required registers; missing versus zero energy; partial/full coverage; nonpositive denominators; signed net flow; reset/rollover; multiplier/power semantics; finite Decimal values and precision; strict IDs/counts; UTC timestamps; enums; version rejection; pagination and filter bounds. A real Kafka round trip validates IDs, UTC time, exact Decimal scale, enum, schema version and correlation. Existing legacy request tests remain part of the suite.
