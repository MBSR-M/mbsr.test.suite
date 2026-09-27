# Streaming and commands

Topics use the `openami.` prefix. Raw meter readings and meter events use six partitions keyed by meter code; transformer readings use three keyed by transformer code. `quality.result`, `aggregate.ready`, `balance.ready` and `anomaly` use three partitions. `meter.interval.ready` and `recompute` use six. `deadletter` has three. Counts are defined centrally in `messaging.TOPICS`; controlled partition changes require an ordering/cutover review.

Envelopes contain event_id, event_type, schema_version, event_time, producer and payload. Internal raw-reading envelopes reference immutable accepted reading IDs in MySQL. This implementation choice makes MySQL raw retention part of replay durability; do not delete raw observations while retained events may reference them. It differs from the production design's self-contained raw payload envelope.

Each logical stage has its own consumer group. Auto-commit is off. SQL inbox/domain/outbox commit precedes Kafka offset commit. Processing is sequential per consumer, and partitions preserve delivery order; event-time calculations resolve current revisions from MySQL. Producer idempotence and acks=all complement database deduplication. No cross-system exactly-once claim is made.

Permanent failures get bounded retries, a MySQL quarantine entry and a deadletter notification before advancing the offset. Database outages do not advance positions and restart the process. Outbox publication failures retain unsent rows and retry. Kafka retention is seven days in local Compose; raw observations and inbox retention must cover the operational replay policy.

RabbitMQ is only a command transport. `/reprocessing-jobs` writes job plus outbox atomically, confirms publication, and manually acknowledges after durable dispatch. QUEUED→DISPATCHED means work was scheduled, not that all calculations finished. Unsupported report/notification command types are not exposed. Single-broker Kafka and RabbitMQ are restartable but not highly available.

CSV and external source topics enter through `python -m opengrid.adapters`. External topic names must not start with `openami.` to prevent feedback loops. Invalid external messages stop that adapter without committing their offsets; operators must resolve or explicitly quarantine them before resuming.
