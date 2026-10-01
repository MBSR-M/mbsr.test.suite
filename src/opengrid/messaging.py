import json
import logging
import time

import pika
from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient, NewTopic
from sqlalchemy import false, select

from opengrid.config import settings
from opengrid.db import Outbox

TOPICS = {
    "meter.reading": 6,
    "transformer.reading": 3,
    "meter.event": 6,
    "quality.result": 3,
    "aggregate.ready": 3,
    "balance.ready": 3,
    "anomaly": 3,
    "deadletter": 3,
    "meter.interval.ready": 6,
    "recompute": 6,
}


def producer():
    return Producer(
        {
            "bootstrap.servers": settings().kafka_bootstrap_servers,
            "enable.idempotence": True,
            "acks": "all",
            "delivery.timeout.ms": 30000,
        }
    )


def rabbit():
    cfg = settings()
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(
            cfg.rabbitmq_host,
            credentials=pika.PlainCredentials(cfg.rabbitmq_user, cfg.rabbitmq_password),
            heartbeat=30,
            blocked_connection_timeout=30,
        )
    )
    channel = connection.channel()
    for name in ("calculation", "reprocessing", "notification"):
        channel.queue_declare(queue=f"openami.{name}", durable=True)
    channel.confirm_delivery()
    return connection, channel


def initialize_topics():
    admin = AdminClient({"bootstrap.servers": settings().kafka_bootstrap_servers})
    existing = admin.list_topics(timeout=30).topics
    missing = [
        NewTopic(f"openami.{name}", count, 1)
        for name, count in TOPICS.items()
        if f"openami.{name}" not in existing
    ]
    futures = admin.create_topics(missing) if missing else {}
    for future in futures.values():
        future.result()
    connection, _ = rabbit()
    connection.close()


def relay_once(factory, owner, client):
    # Single relay per service in MVP. SKIP LOCKED also prevents simultaneous duplicate claims.
    # Transaction remains open until bounded broker confirm; any crash rolls back sent marking.
    with factory.begin() as s:
        rows = s.scalars(
            select(Outbox)
            .where(Outbox.owner == owner, Outbox.sent == false())
            .order_by(Outbox.created_at, Outbox.id)
            .limit(100)
            .with_for_update(skip_locked=True)
            # MySQL otherwise may prefer either single-column index and lock a
            # large historical range before it reaches the bounded batch.
            .with_hint(
                Outbox,
                "FORCE INDEX (ix_outbox_owner_sent_created_at_id)",
                dialect_name="mysql",
            )
        ).all()
        for row in rows:
            body = json.dumps(row.payload).encode()
            if row.topic.startswith("rabbit:"):
                connection, channel = rabbit()
                try:
                    channel.basic_publish(
                        "",
                        row.topic[7:],
                        body,
                        pika.BasicProperties(delivery_mode=2, message_id=row.id),
                        mandatory=True,
                    )
                finally:
                    connection.close()
            else:
                errors = []
                client.produce(
                    row.topic,
                    key=row.key,
                    value=body,
                    on_delivery=lambda error, msg, errors=errors: (
                        errors.append(error) if error else None
                    ),
                )
                if client.flush(35) or errors:
                    raise RuntimeError("Kafka publish not confirmed")
            row.sent = True
    return len(rows)


def relay_loop(factory, owner, stop):
    client = producer()
    while not stop.is_set():
        try:
            count = relay_once(factory, owner, client)
            if not count:
                stop.wait(0.5)
        except Exception:
            logging.exception("outbox relay failed", extra={"service": owner})
            stop.wait(5)
    client.flush(10)


def retry_delay(attempt):
    time.sleep(min(2**attempt, 15))
