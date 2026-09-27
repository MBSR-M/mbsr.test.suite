"""Bounded operator diagnostics and durable, resumable demo generation."""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from confluent_kafka import Consumer, ConsumerGroupTopicPartitions
from confluent_kafka.admin import AdminClient
from sqlalchemy import func, select, text

from opengrid.config import settings
from opengrid.contracts import ReadingInput
from opengrid.db import Anomaly, Asset, Assignment, Case, Configuration, Feeder, Job, now
from opengrid.domain import STEP
from opengrid.messaging import rabbit
from opengrid.services import accept_reading, emit
from opengrid.ui_queries import system_data, value

SCENARIOS = {"normal", "missing-data", "communication-outage", "consumption-drop", "transformer-imbalance", "combined"}


def _kafka_status():
    cfg = settings()
    admin = AdminClient({"bootstrap.servers": cfg.kafka_bootstrap_servers, "socket.timeout.ms": 2000})
    admin.list_topics(timeout=2)
    lag = 0
    consumer = Consumer({"bootstrap.servers": cfg.kafka_bootstrap_servers, "group.id": "opengrid-ui-diagnostics", "enable.auto.commit": False})
    try:
        for role in ("quality", "aggregation", "loss", "anomaly"):
            groups = admin.list_consumer_group_offsets([ConsumerGroupTopicPartitions(f"opengrid-{role}-v1")])
            offsets = next(iter(groups.values())).result(timeout=2).topic_partitions
            for partition in offsets[:48]:
                if partition.offset >= 0:
                    _, high = consumer.get_watermark_offsets(partition, timeout=1)
                    lag += max(0, high - partition.offset)
    finally:
        consumer.close()
    return {"service": "Kafka", "status": "HEALTHY", "detail": "Broker and consumer positions reachable", "lag": lag}


def _rabbit_status():
    connection, channel = rabbit()
    try:
        depth = sum(channel.queue_declare(queue=f"openami.{name}", durable=True, passive=True).method.message_count
                    for name in ("calculation", "reprocessing", "notification"))
        return {"service": "RabbitMQ", "status": "HEALTHY", "detail": "Command queues reachable", "depth": depth}
    finally:
        connection.close()


def _grafana_status():
    response = httpx.get("http://grafana:3000/api/health", timeout=2)
    response.raise_for_status()
    return {"service": "Grafana", "status": "HEALTHY" if response.json().get("database") == "ok" else "DEGRADED", "detail": "Health endpoint reached"}


def system_status(s):
    data = system_data(s)
    def probe(name, function):
        try:
            return function()
        except Exception:
            return {"service": name, "status": "DOWN", "detail": "Health probe failed; inspect service logs"}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(probe, name, fn) for name, fn in (("Kafka", _kafka_status), ("RabbitMQ", _rabbit_status), ("Grafana", _grafana_status))]
        checks = [f.result() for f in futures]
    data["services"] = data["services"][:2] + checks
    data["kafka_consumer_lag"] = checks[0].get("lag")
    data["rabbitmq_queue_depth"] = checks[1].get("depth")
    data["database_connections"] = int(s.execute(text("SHOW STATUS LIKE 'Threads_connected'")).one()[1])
    data["status"] = "DEGRADED" if any(x["status"] != "HEALTHY" for x in data["services"] + data["workers"]) else data["status"]
    data["telemetry_note"] = "Broker lag and queue depth are live probes. Worker counts are cumulative; a processing rate is not inferred without two samples."
    return data


def start_simulation(s, username, scenario, transformers, meters, days, action="start"):
    if not settings().ui_demo_enabled:
        raise ValueError("Simulation is disabled for this deployment")
    if action not in {"start", "demo", "quick", "reset", "inject"} or scenario not in SCENARIOS:
        raise ValueError("Unsupported simulation action or scenario")
    if action == "demo":
        transformers, meters, days, scenario = 100, 100, 29, "combined"
    elif action in {"quick", "reset", "inject"}:
        transformers, meters, days, scenario = 4, 5, 29, "combined"
    if not 1 <= transformers <= 100 or not 1 <= meters <= 100 or not 1 <= days <= 31:
        raise ValueError("Use 1–100 transformers, 1–100 meters each, and 1–31 days")
    active = s.scalar(select(Job).where(Job.status.in_(["SIMULATION_QUEUED", "SIMULATION_RUNNING"])).limit(1).with_for_update())
    if active:
        if action == "reset":
            active.status = "SIMULATION_STOPPED"
        else:
            raise ValueError("A simulation is already running. Stop it before starting another.")
    job_id = str(uuid4())
    # Eight adjacent slots/day provide slot-comparable history and a persistent final event.
    end = now().replace(minute=(now().minute // 15) * 15, second=0, microsecond=0) - STEP
    payload = {"kind": "SIMULATION", "actor": username, "scenario": scenario,
               "transformers": transformers, "meters_per_transformer": meters, "days": days,
               "stage": "TOPOLOGY", "topology_cursor": 0, "cursor": 0, "generated": 0, "skipped": 0,
               "total": transformers * (meters + 1) * days * 8,
               "end": end.isoformat(), "prefix": f"SIM-{job_id[:8]}", "interval_minutes": 15}
    s.add(Job(id=job_id, status="SIMULATION_QUEUED", payload=payload))
    return job_id


def _code(p, transformer, meter=None):
    return f"{p['prefix']}-DT-{1047 + transformer}" if meter is None else f"{p['prefix']}-M-{transformer:03d}-{meter:03d}"


def _topology_chunk(s, job, p):
    count = p["transformers"] * (p["meters_per_transformer"] + 1)
    start = datetime.fromisoformat(p["end"]) - timedelta(days=p["days"]) - 8 * STEP
    feeder = s.scalar(select(Feeder).where(Feeder.code == p["prefix"] + "-FDR"))
    if not feeder:
        feeder = Feeder(code=p["prefix"] + "-FDR", name="Simulation feeder")
        s.add(feeder)
        s.flush()
    cursor = p["topology_cursor"]
    for index in range(cursor, min(cursor + 100, count)):
        transformer_index, meter_index = divmod(index, p["meters_per_transformer"] + 1)
        kind = "TRANSFORMER" if meter_index == 0 else "METER"
        asset = Asset(code=_code(p, transformer_index, None if meter_index == 0 else meter_index - 1), kind=kind, feeder_id=feeder.id,
                      name=f"Demo {kind.lower()} · {p['scenario']}")
        s.add(asset)
        s.flush()
        s.add(Configuration(asset_id=asset.id, valid_from=start, multiplier=1, import_only=True))
        if meter_index:
            owner = s.scalar(select(Asset.id).where(Asset.code == _code(p, transformer_index)))
            s.add(Assignment(meter_id=asset.id, transformer_id=owner, valid_from=start))
    p["topology_cursor"] = min(cursor + 100, count)
    if p["topology_cursor"] == count:
        p["stage"] = "READINGS"


def _readings_chunk(s, job, p):
    assets_per_slot = p["transformers"] * (p["meters_per_transformer"] + 1)
    end = datetime.fromisoformat(p["end"]).replace(tzinfo=UTC)
    for index in range(p["cursor"], min(p["cursor"] + 200, p["total"])):
        block, within = divmod(index, assets_per_slot)
        day, slot = divmod(block, 8)
        ti, mi = divmod(within, p["meters_per_transformer"] + 1)
        boundary = end - timedelta(days=p["days"] - 1 - day) - (7 - slot) * STEP
        changed = day == p["days"] - 1
        scenario = p["scenario"]
        if scenario == "combined":
            scenario = ("transformer-imbalance", "missing-data", "consumption-drop", "communication-outage")[ti % 4]
        code = _code(p, ti, None if mi == 0 else mi - 1)
        missing = changed and mi > 0 and (scenario == "communication-outage" or (scenario == "missing-data" and mi == 1))
        if missing:
            p["skipped"] = p.get("skipped", 0) + 1
            continue
        affected = min(4, p["meters_per_transformer"])
        baseline = Decimal("1.5") + Decimal(slot) / 100
        drop = changed and scenario in {"consumption-drop", "transformer-imbalance"}
        meter_value = baseline * Decimal("0.1") if drop and 1 <= mi <= affected else baseline
        downstream = baseline * (p["meters_per_transformer"] - affected) + baseline * Decimal("0.1") * affected if drop else baseline * p["meters_per_transformer"]
        imbalance = Decimal("0.187") if changed and scenario == "transformer-imbalance" else Decimal("0.072")
        measurement = downstream / (1 - imbalance) if mi == 0 else meter_value
        item = ReadingInput(event_id=uuid5(NAMESPACE_URL, f"{job.id}/{index}"), entity_code=code,
             measurement_kind="INTERVAL_ENERGY", reading_time=boundary, interval_start=boundary-STEP,
             import_energy_kwh=measurement.quantize(Decimal("0.000001")), export_energy_kwh=Decimal(0),
             energy_basis="PRIMARY", source="UI_SIMULATOR", received_at=boundary)
        # This loop is hosted by the API service, whose relay owns API outbox
        # records.  The normal ingestion endpoint continues to use its own
        # owner through the default in ``accept_reading``.
        accept_reading(s, item, outbox_owner="api")
        p["generated"] += 1
        if changed and slot == 0 and mi in {1, 2} and scenario in {"transformer-imbalance", "communication-outage"}:
            meter_id = s.scalar(select(Asset.id).where(Asset.code == code))
            emit(s, "api", "openami.meter.event", code, {"asset_id": meter_id, "time": boundary.replace(tzinfo=None).isoformat(),
                 "event_type": "COMMUNICATION_FAILURE" if scenario == "communication-outage" else "COVER_OPEN", "evidence": {"source": "synthetic demonstration"}})
    p["cursor"] = min(p["cursor"] + 200, p["total"])
    if p["cursor"] == p["total"]:
        p["stage"], job.status = "ACCEPTED", "SIMULATION_COMPLETE"


def simulation_loop(factory, stop):
    while not stop.wait(0.25):
        job_id = None
        try:
            with factory.begin() as s:
                job = s.scalar(select(Job).where(Job.status.in_(["SIMULATION_QUEUED", "SIMULATION_RUNNING"]))
                    .order_by(Job.created_at).limit(1).with_for_update(skip_locked=True))
                if not job:
                    continue
                job_id = job.id
                payload = dict(job.payload)
                job.status = "SIMULATION_RUNNING"
                if payload["stage"] == "TOPOLOGY":
                    _topology_chunk(s, job, payload)
                else:
                    _readings_chunk(s, job, payload)
                job.payload = payload
        except Exception:
            logging.exception("simulation failed job_id=%s", job_id)
            if job_id:
                try:
                    with factory.begin() as s:
                        job = s.get(Job, job_id, with_for_update=True)
                        if job:
                            job.status, job.error = "SIMULATION_FAILED", "Generation failed; inspect operator service logs using the job ID."
                except Exception:
                    logging.exception("could not record simulation failure")
            stop.wait(3)


def simulation_status(s):
    jobs = s.scalars(select(Job).where(Job.status.like("SIMULATION_%")).order_by(Job.created_at.desc()).limit(20)).all()
    result = []
    for job in jobs:
        prefix = job.payload["prefix"]
        scope = select(Asset.id).where(Asset.code.startswith(prefix, autoescape=True))
        result.append({"id": job.id, "status": job.status, "payload": job.payload, "created_at": value(job.created_at), "error": job.error,
            "processed": job.payload.get("cursor", 0),
            "skipped": job.payload.get("skipped", max(0, job.payload.get("cursor", 0) - job.payload.get("generated", 0))),
            "anomalies": s.scalar(select(func.count()).select_from(Anomaly).where(Anomaly.asset_id.in_(scope))),
            "investigations": s.scalar(select(func.count()).select_from(Case).where(Case.asset_id.in_(scope)))})
    return result
