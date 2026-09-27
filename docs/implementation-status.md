# Implemented MVP versus target design

The root design document is the production target. This repository implements the runnable accounting and investigation path, with the following deliberate simplifications visible in code and tests.

| Area | Implemented behavior |
|---|---|
| Persistence | Shared typed asset table with FKs; immutable readings; current interval/balance projections with archived prior evidence |
| Configuration | Effective dated initial measurement configuration; API assignment history; configuration revision administration remains a follow-up |
| Accounting | Register and interval input; Decimal scaling; explicit export policy; strict 100% valid population for a reported imbalance percentage |
| Normalization | 15-minute aligned input only; no interpolation; explicit epoch and verified rollover bounds |
| Reliability | Transactional inbox/outbox; sequential Kafka processing; row locking and READ COMMITTED snapshots; transactional downstream events |
| Outbox | Bounded publish-confirm transaction holds row locks; production design's separate lease-based relay is not yet implemented |
| Corrections | Neighbor intervals and affected later baseline/persistence slots recomputed; prior evidence archived; human decisions preserved |
| Detection | Median/MAD same-slot baseline, 4-of-6 persistence, consumption correlation, scoring and separate confidence index |
| Meter anomalies | Independent meter interval notifications evaluate consumption/flatline/zero findings; transformer findings correlate them with imbalance evidence |
| Cases | One active case per transformer under asset locking; original evidence and versioned human audit; full episode lifecycle remains a follow-up |
| RabbitMQ | Confirmed, manually acknowledged reprocessing dispatch; DISPATCHED does not imply completed calculation |
| Security | Separate read/write API keys, loopback exposure, SELECT-only Grafana views; per-user identity/TLS/service-specific secrets remain production gates |
| Grafana | Seven provisioned dashboards; worker heartbeats, accounting, quality and case drilldown; detailed broker lag/latency exporters remain a follow-up |
| Scale | Full-size simulator supported; large-scale throughput, retention and restore SLOs not yet measured |

These distinctions prevent a working local MVP from being mistaken for a certified production deployment. See the test results in README for the checks actually executed.
