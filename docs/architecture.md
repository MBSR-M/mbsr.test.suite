# Architecture

See the root [production design](../OPEN_GRID_LOSS_DESIGN.md) for service boundaries, reliability decisions, accounting semantics, diagrams, capacity reasoning and acceptance gates. The [implementation status](implementation-status.md) records the exact MVP simplifications.

One application image runs API, ingestion, quality, aggregation, loss and anomaly entry points. All services share domain code; each stage owns its writes and transactional outbox. MySQL is the authoritative record, Kafka carries event notifications, RabbitMQ carries commands, and Grafana queries read-only reporting views. There are no external paid services.
