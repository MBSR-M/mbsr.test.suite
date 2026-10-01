import json
import logging
import signal
import sys
import threading
from datetime import datetime, timedelta
from uuid import uuid4

from confluent_kafka import Consumer
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from opengrid.config import settings
from opengrid.db import (
    Asset,
    Assignment,
    Heartbeat,
    Inbox,
    Job,
    MeterEvent,
    Quarantine,
    now,
    session_factory,
)
from opengrid.domain import STEP
from opengrid.messaging import rabbit, relay_loop
from opengrid.services import (
    aggregate_handler,
    anomaly_handler,
    digest,
    emit,
    loss_handler,
    meter_anomaly_handler,
    normalize,
    quality_handler,
)

HANDLERS = {
    "quality": quality_handler,
    "aggregation": aggregate_handler,
    "loss": loss_handler,
    "anomaly": anomaly_handler,
}
SUBSCRIPTIONS = {
    "quality": ["meter.reading", "transformer.reading", "meter.event", "recompute"],
    "aggregation": ["quality.result"],
    "loss": ["aggregate.ready"],
    "anomaly": ["balance.ready", "meter.interval.ready"],
}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        result = {
            "timestamp": now().isoformat() + "Z",
            "level": record.levelname,
            "service": settings().service,
            "message": record.getMessage(),
        }
        if record.exc_info:
            result["exception"] = self.formatException(record.exc_info)
        return json.dumps(result)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=settings().log_level, handlers=[handler], force=True)


def process(factory, role, envelope, topic):
    event_id = envelope["event_id"]
    if envelope.get("schema_version") != 1:
        raise ValueError("unsupported schema version")
    with factory.begin() as s:
        previous = s.get(Inbox, (role, event_id))
        if previous:
            if previous.digest != digest(envelope):
                raise ValueError("event ID payload conflict")
            return
        s.add(Inbox(consumer=role, event_id=event_id, digest=digest(envelope)))
        s.flush()
        payload = envelope["payload"]
        if topic == "openami.meter.event":
            if not s.get(MeterEvent, event_id):
                s.add(
                    MeterEvent(
                        id=event_id,
                        asset_id=payload["asset_id"],
                        time=datetime.fromisoformat(payload["time"]),
                        event_type=payload["event_type"],
                        evidence=payload.get("evidence", {}),
                    )
                )
        elif topic == "openami.recompute":
            normalize(s, payload["asset_id"], datetime.fromisoformat(payload["start"]))
            # Force downstream recalculation even if normalization did not change.
            for owner in payload.get("owners", []):
                emit(
                    s,
                    "quality",
                    "openami.quality.result",
                    owner,
                    {"asset_id": owner, "start": payload["start"]},
                )
        elif topic == "openami.meter.interval.ready":
            meter_anomaly_handler(s, payload)
        else:
            HANDLERS[role](s, payload)


def schedule_range(s, transformer_id, start, end):
    if not s.scalar(
        select(Asset.id).where(Asset.id == transformer_id, Asset.kind == "TRANSFORMER")
    ):
        raise ValueError("unknown transformer")
    time = start
    while time < end:
        members = s.scalars(
            select(Assignment.meter_id).where(
                Assignment.transformer_id == transformer_id,
                Assignment.valid_from < time + STEP,
                (Assignment.valid_to.is_(None) | (Assignment.valid_to > time)),
            )
        ).all()
        for asset_id in [transformer_id, *set(members)]:
            emit(
                s,
                "loss",
                "openami.recompute",
                asset_id,
                {"asset_id": asset_id, "start": time.isoformat(), "owners": [transformer_id]},
            )
        time += STEP


def command_loop(factory, stop):
    while not stop.is_set():
        connection = None
        try:
            connection, channel = rabbit()
            channel.basic_qos(prefetch_count=1)
            for method, _, body in channel.consume("openami.reprocessing", inactivity_timeout=1):
                if stop.is_set():
                    break
                if not method:
                    continue
                envelope = json.loads(body)
                job_id = envelope["payload"]["job_id"]
                with factory.begin() as s:
                    job = s.get(Job, job_id, with_for_update=True)
                    if not job:
                        raise ValueError("job not durably registered")
                    if job.status == "QUEUED":
                        payload = job.payload
                        schedule_range(
                            s,
                            payload["transformer_id"],
                            datetime.fromisoformat(payload["start_time"]).replace(tzinfo=None),
                            datetime.fromisoformat(payload["end_time"]).replace(tzinfo=None),
                        )
                        job.status = "DISPATCHED"
                channel.basic_ack(method.delivery_tag)
            channel.cancel()
        except Exception:
            logging.exception("command consumer failed")
            stop.wait(5)
        finally:
            if connection and connection.is_open:
                connection.close()


def reconciliation_loop(factory, stop):
    while not stop.wait(60):
        try:
            with factory.begin() as s:
                # Durable checkpoint and locking ensure only one scheduler advances each slot.
                checkpoint = s.get(Heartbeat, "scheduler", with_for_update=True)
                deadline = now() - timedelta(minutes=settings().allowed_lateness_minutes)
                boundary = deadline.replace(
                    minute=(deadline.minute // 15) * 15, second=0, microsecond=0
                )
                if not checkpoint:
                    checkpoint = Heartbeat(service="scheduler", time=boundary - STEP, processed=0)
                    s.add(checkpoint)
                # Reconciliation is a near-real-time safety net, not an
                # unbounded historical backfill. A stale checkpoint would
                # otherwise create a growing recompute backlog after downtime.
                if checkpoint.time < boundary - 4 * STEP:
                    logging.warning(
                        "scheduler checkpoint was stale; resuming recent window",
                        extra={"service": "scheduler"},
                    )
                    checkpoint.time = boundary - 4 * STEP
                start = checkpoint.time
                end = min(boundary, start + 4 * STEP)
                transformers = s.scalars(
                    select(Asset.id).where(Asset.kind == "TRANSFORMER", Asset.active.is_(True))
                ).all()
                for transformer_id in transformers:
                    schedule_range(s, transformer_id, start, end)
                checkpoint.time = end
        except Exception:
            logging.exception("reconciliation failed")


def main():
    role = sys.argv[1]
    configure_logging()
    factory = session_factory()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    threads = [threading.Thread(target=relay_loop, args=(factory, role, stop), daemon=True)]
    if role == "loss":
        threads += [
            threading.Thread(target=command_loop, args=(factory, stop), daemon=True),
            threading.Thread(target=reconciliation_loop, args=(factory, stop), daemon=True),
        ]
    for thread in threads:
        thread.start()
    consumer = Consumer(
        {
            "bootstrap.servers": settings().kafka_bootstrap_servers,
            "group.id": f"opengrid-{role}-v1",
            "enable.auto.commit": False,
            "auto.offset.reset": settings().kafka_auto_offset_reset,
            "max.poll.interval.ms": 900000,
        }
    )
    consumer.subscribe([f"openami.{t}" for t in SUBSCRIPTIONS[role]])
    processed = 0
    last_heartbeat = now() - timedelta(minutes=1)
    try:
        while not stop.is_set():
            if now() - last_heartbeat > timedelta(seconds=10):
                with factory.begin() as s:
                    s.merge(Heartbeat(service=role, time=now(), processed=processed))
                last_heartbeat = now()
            message = consumer.poll(1)
            if message is None:
                continue
            if message.error():
                logging.error("Kafka error: %s", message.error())
                continue
            for attempt in range(settings().max_retries):
                try:
                    envelope = json.loads(message.value())
                    process(factory, role, envelope, message.topic())
                    consumer.commit(message=message, asynchronous=False)
                    processed += 1
                    break
                except OperationalError:
                    logging.exception("database unavailable; retaining Kafka position")
                    if attempt == settings().max_retries - 1:
                        raise
                    stop.wait(min(2**attempt, 15))
                except Exception as error:
                    logging.exception("processing failure")
                    if attempt < settings().max_retries - 1:
                        stop.wait(min(2**attempt, 15))
                        continue
                    with factory.begin() as s:
                        payload = {
                            "raw": message.value().decode(errors="replace"),
                            "topic": message.topic(),
                            "partition": message.partition(),
                            "offset": message.offset(),
                        }
                        s.add(Quarantine(service=role, reason=str(error)[:2000], payload=payload))
                        emit(
                            s,
                            role,
                            "openami.deadletter",
                            str(uuid4()),
                            {"reason": type(error).__name__},
                        )
                    consumer.commit(message=message, asynchronous=False)
    finally:
        stop.set()
        consumer.close()
        for thread in threads:
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
