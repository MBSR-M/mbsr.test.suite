import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import or_, select

from opengrid.config import settings
from opengrid.db import (
    Aggregate,
    Anomaly,
    Asset,
    Assignment,
    Audit,
    Balance,
    Case,
    Configuration,
    Evaluation,
    Interval,
    MeterEvent,
    Outbox,
    ProjectionRevision,
    Reading,
    now,
)
from opengrid.domain import (
    STEP,
    calculate_anomaly_score,
    calculate_baseline,
    calculate_energy_balance,
    consumption_deviation,
    detect_persistent_anomaly,
    energy,
    register_delta,
)

D = Decimal
VALID = {"VALID", "REGISTER_ROLLOVER"}


class Conflict(ValueError):
    pass


def digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def emit(s, owner, topic, key, payload):
    event_id = str(uuid4())
    s.add(
        Outbox(
            id=event_id,
            owner=owner,
            topic=topic,
            key=str(key),
            payload={
                "event_id": event_id,
                "event_type": topic.removeprefix("openami."),
                "schema_version": 1,
                "event_time": datetime.now(UTC).isoformat(),
                "producer": owner,
                "payload": payload,
            },
        )
    )


def record(s, model, asset_id, start):
    return s.scalar(select(model).where(model.asset_id == asset_id, model.start == start))


def archive(s, kind, asset_id, start, body):
    s.add(ProjectionRevision(kind=kind, asset_id=asset_id, start=start, body=body))


def accept_reading(s, item, expected_kind=None, *, outbox_owner="ingestion"):
    """Persist a reading and queue its event for the service that accepted it.

    Ingestion remains the default owner for the public ingestion API.  The
    browser simulator runs inside the API process, so it explicitly uses the
    API relay instead of leaving events stranded in the ingestion outbox.
    """
    asset = s.scalar(select(Asset).where(Asset.code == item.entity_code).with_for_update())
    if not asset or (expected_kind and asset.kind != expected_kind):
        raise ValueError("unknown entity or incorrect entity kind")
    body = item.model_dump(mode="json")
    body_hash = digest(body)
    existing = s.scalar(select(Reading).where(Reading.event_id == str(item.event_id)))
    if existing:
        if existing.digest != body_hash:
            raise Conflict("event_id already used with different payload")
        return existing.event_id
    time = item.reading_time.replace(tzinfo=None)
    other = s.scalar(
        select(Reading).where(
            Reading.asset_id == asset.id,
            Reading.time == time,
            Reading.source == item.source,
            Reading.revision == item.revision,
        )
    )
    if other:
        old = dict(other.payload)
        old["event_id"] = body["event_id"]
        if digest(old) != body_hash:
            raise Conflict("natural observation key already has different data")
        return other.event_id
    # MVP uses exactly one authoritative source per asset; no silent source selection.
    source = s.scalar(select(Reading.source).where(Reading.asset_id == asset.id).limit(1))
    if source is not None and source != item.source:
        raise Conflict("asset already has an authoritative source")
    cumulative = item.measurement_kind == "CUMULATIVE_REGISTER"
    row = Reading(
        event_id=str(item.event_id),
        asset_id=asset.id,
        time=time,
        source=item.source,
        revision=item.revision,
        kind=item.measurement_kind,
        epoch=item.register_epoch,
        primary=item.energy_basis == "PRIMARY",
        import_value=item.import_energy_total_kwh if cumulative else item.import_energy_kwh,
        export_value=item.export_energy_total_kwh if cumulative else item.export_energy_kwh,
        received_at=(item.received_at or datetime.now(UTC)).replace(tzinfo=None),
        payload=body,
        digest=body_hash,
    )
    s.add(row)
    s.flush()
    emit(
        s, outbox_owner, f"openami.{asset.kind.lower()}.reading", asset.code, {"reading_id": row.id}
    )
    return row.event_id


def effective(s, model, asset_id, start):
    return s.scalar(
        select(model).where(
            model.asset_id == asset_id,
            model.valid_from <= start,
            or_(model.valid_to.is_(None), model.valid_to >= start + STEP),
        )
    )


def latest(s, asset_id, time):
    return s.scalar(
        select(Reading)
        .where(Reading.asset_id == asset_id, Reading.time == time)
        .order_by(Reading.revision.desc())
        .limit(1)
    )


def normalize(s, asset_id, start):
    asset = s.get(Asset, asset_id, with_for_update=True)
    cfg = effective(s, Configuration, asset_id, start)
    left, right = latest(s, asset_id, start), latest(s, asset_id, start + STEP)
    evidence = {
        "algorithm": "normalize-v1",
        "configuration_id": cfg.id if cfg else None,
        "reading_ids": [r.id for r in (left, right) if r],
        "flags": [],
    }
    imp = exp = None
    status = "MISSING"
    if right and cfg:
        factor = D(1) if right.primary else cfg.multiplier
        limit = cfg.max_power_kw / 4 if cfg.max_power_kw else None
        if right.kind == "INTERVAL_ENERGY":
            imp = energy(right.import_value * factor)
            exp = (
                energy(right.export_value * factor)
                if right.export_value is not None
                else (D(0) if cfg.import_only else None)
            )
            status = (
                "VALID"
                if exp is not None and (limit is None or max(imp, exp) <= limit)
                else "INVALID"
            )
        elif left and left.kind == right.kind and left.primary == right.primary:
            result = register_delta(
                left.import_value,
                right.import_value,
                factor,
                same_epoch=left.epoch == right.epoch,
                modulus=cfg.modulus,
                rollover_confirmed=right.payload.get("rollover_confirmed", False),
                max_energy=limit,
            )
            imp, status = result.value, result.status
            if left.export_value is not None and right.export_value is not None:
                result_exp = register_delta(
                    left.export_value,
                    right.export_value,
                    factor,
                    same_epoch=left.epoch == right.epoch,
                    modulus=cfg.modulus,
                    rollover_confirmed=right.payload.get("rollover_confirmed", False),
                    max_energy=limit,
                )
                exp = result_exp.value
                if exp is None:
                    status = result_exp.status
            elif cfg.import_only:
                exp = D(0)
            else:
                status = "UNKNOWN_EXPORT"
        if (
            right.received_at - right.time
        ).total_seconds() > settings().allowed_lateness_minutes * 60:
            evidence["flags"].append("LATE")
        if left and right.ingested_at < left.ingested_at:
            evidence["flags"].append("OUT_OF_ORDER")
    if not cfg:
        status = "MISSING_CONFIGURATION"
    evidence.update(
        import_kwh=str(imp) if imp is not None else None,
        export_kwh=str(exp) if exp is not None else None,
        status=status,
        avg_import_power_kw=str(imp * 4) if imp is not None else None,
    )
    current = record(s, Interval, asset_id, start)
    if current and current.evidence == evidence:
        return
    if current:
        archive(s, "interval", asset_id, start, current.evidence)
        current.revision += 1
    else:
        current = Interval(asset_id=asset_id, start=start)
        s.add(current)
    current.import_kwh, current.export_kwh, current.status = imp, exp, status
    current.evidence, current.processed_at = evidence, now()
    s.flush()
    if asset.kind == "TRANSFORMER":
        owners = [asset_id]
    else:
        owners = s.scalars(
            select(Assignment.transformer_id).where(
                Assignment.meter_id == asset_id,
                Assignment.valid_from < start + STEP,
                or_(Assignment.valid_to.is_(None), Assignment.valid_to > start),
            )
        ).all()
    for owner in set(owners):
        emit(
            s,
            "quality",
            "openami.quality.result",
            owner,
            {"asset_id": owner, "start": start.isoformat()},
        )
    if asset.kind == "METER":
        emit(
            s,
            "quality",
            "openami.meter.interval.ready",
            asset_id,
            {"asset_id": asset_id, "start": start.isoformat(), "revision": current.revision},
        )


def quality_handler(s, payload):
    row = s.get(Reading, payload["reading_id"])
    normalize(s, row.asset_id, row.time - STEP)
    normalize(s, row.asset_id, row.time)


def aggregate_handler(s, payload):
    asset_id, start = payload["asset_id"], datetime.fromisoformat(payload["start"])
    s.get(Asset, asset_id, with_for_update=True)
    assignments = s.scalars(
        select(Assignment).where(
            Assignment.transformer_id == asset_id,
            Assignment.valid_from <= start,
            or_(Assignment.valid_to.is_(None), Assignment.valid_to >= start + STEP),
        )
    ).all()
    ids = sorted({a.meter_id for a in assignments})
    rows = (
        s.scalars(select(Interval).where(Interval.asset_id.in_(ids), Interval.start == start)).all()
        if ids
        else []
    )
    valid = [
        r
        for r in rows
        if r.status in VALID and r.import_kwh is not None and r.export_kwh is not None
    ]
    downstream = sum((r.import_kwh - r.export_kwh for r in valid), D(0))
    transformer = record(s, Interval, asset_id, start)
    body = {
        "expected_meter_ids": ids,
        "valid_meter_ids": [r.asset_id for r in valid],
        "interval_revisions": [[r.id, r.revision] for r in rows],
        "transformer_revision": transformer.revision if transformer else None,
        "downstream_kwh": str(downstream),
    }
    fingerprint = digest(body)
    current = record(s, Aggregate, asset_id, start)
    if current and current.fingerprint == fingerprint:
        return
    if not current:
        current = Aggregate(asset_id=asset_id, start=start)
        s.add(current)
    current.expected, current.received, current.valid = (
        len(ids),
        sum(r.status != "MISSING" for r in rows),
        len(valid),
    )
    current.downstream_kwh, current.fingerprint, current.evidence = downstream, fingerprint, body
    emit(s, "aggregation", "openami.aggregate.ready", asset_id, payload)


def loss_handler(s, payload):
    asset_id, start = payload["asset_id"], datetime.fromisoformat(payload["start"])
    s.get(Asset, asset_id, with_for_update=True)
    agg = record(s, Aggregate, asset_id, start)
    if not agg:
        return
    transformer = record(s, Interval, asset_id, start)
    t = (
        transformer.import_kwh - transformer.export_kwh
        if transformer and transformer.status in VALID
        else None
    )
    result = calculate_energy_balance(t, agg.downstream_kwh, agg.expected, agg.valid)
    body = {
        **agg.evidence,
        "expected_meter_count": agg.expected,
        "valid_meter_count": agg.valid,
        "received_meter_count": agg.received,
        "input_kwh": str(t) if t is not None else None,
        "accounting_difference_kwh": str(result.difference)
        if result.difference is not None
        else None,
        "accounting_difference_percent": str(result.percent)
        if result.percent is not None
        else None,
        "status": result.status,
        "algorithm": "balance-v1",
    }
    fingerprint = digest(body)
    current = record(s, Balance, asset_id, start)
    if current and current.fingerprint == fingerprint:
        return
    if current:
        archive(s, "balance", asset_id, start, current.evidence)
    else:
        current = Balance(asset_id=asset_id, start=start)
        s.add(current)
    current.input_kwh, current.downstream_kwh = t, agg.downstream_kwh
    current.accounting_difference_kwh, current.accounting_difference_percent = (
        result.difference,
        result.percent,
    )
    current.completeness, current.status = result.completeness, result.status
    current.fingerprint, current.evidence = fingerprint, body
    emit(s, "loss", "openami.balance.ready", asset_id, payload)


def evaluate(s, asset_id, start):
    cfg = settings()
    current = record(s, Balance, asset_id, start)
    if not current:
        return
    history = s.scalars(
        select(Balance)
        .where(
            Balance.asset_id == asset_id,
            Balance.start >= start - timedelta(days=cfg.baseline_days),
            Balance.start < start,
            Balance.status == "COMPLETE",
        )
        .order_by(Balance.start)
    ).all()
    history = [
        b
        for b in history
        if b.start.time() == start.time()
        and b.evidence["expected_meter_ids"] == current.evidence["expected_meter_ids"]
        and b.accounting_difference_percent is not None
    ]
    baseline = calculate_baseline(
        [b.accounting_difference_percent for b in history], cfg.baseline_min_samples
    )
    eligible = (
        current.status == "COMPLETE"
        and current.accounting_difference_percent is not None
        and baseline is not None
    )
    deviation = current.accounting_difference_percent - baseline["median"] if eligible else D(0)
    threshold = (
        max(D(str(cfg.deviation_pp)), 3 * D("1.4826") * baseline["mad"]) if baseline else D(0)
    )
    abnormal = bool(
        eligible
        and deviation > threshold
        and current.accounting_difference_kwh > D(str(cfg.min_impact_kwh))
    )
    evidence = {
        **current.evidence,
        "balance_fingerprint": current.fingerprint,
        "baseline": {k: str(v) for k, v in baseline.items()} if baseline else None,
        "historical_samples": len(history),
        "deviation_pp": str(deviation),
        "threshold_pp": str(threshold),
        "eligible": eligible,
        "suppression_reason": None if eligible else "INCOMPLETE_OR_INSUFFICIENT_HISTORY",
        "algorithm": "anomaly-v1",
    }
    evaluation = record(s, Evaluation, asset_id, start)
    if not evaluation:
        evaluation = Evaluation(asset_id=asset_id, start=start)
        s.add(evaluation)
    evaluation.eligible, evaluation.abnormal, evaluation.evidence = eligible, abnormal, evidence
    s.flush()
    slots = s.scalars(
        select(Evaluation)
        .where(
            Evaluation.asset_id == asset_id,
            Evaluation.start >= start - 5 * STEP,
            Evaluation.start <= start,
        )
        .order_by(Evaluation.start)
    ).all()
    persistent = detect_persistent_anomaly(
        [(r.start, r.eligible, r.abnormal) for r in slots], cfg.persistence_required
    )
    prior = s.scalar(
        select(Anomaly).where(
            Anomaly.asset_id == asset_id,
            Anomaly.start == start,
            Anomaly.anomaly_type == "ENERGY_IMBALANCE",
        )
    )
    if not persistent:
        if prior and prior.status != "SUPERSEDED":
            archive(s, "anomaly", asset_id, start, prior.evidence)
            prior.status = "SUPERSEDED"
        return
    abnormal_meters = []
    for meter_id in current.evidence["valid_meter_ids"]:
        interval = record(s, Interval, meter_id, start)
        meter_history = s.scalars(
            select(Interval).where(
                Interval.asset_id == meter_id,
                Interval.start >= start - timedelta(days=28),
                Interval.start < start,
                Interval.status.in_(VALID),
            )
        ).all()
        values = [r.import_kwh for r in meter_history if r.start.time() == start.time()]
        kind = consumption_deviation(interval.import_kwh, values)
        recent = sorted(meter_history, key=lambda r: r.start)[-5:] + [interval]
        if len(recent) == 6 and all(
            b.start - a.start == STEP for a, b in zip(recent, recent[1:], strict=False)
        ):
            if all(r.import_kwh == 0 for r in recent):
                kind = "ZERO_CONSUMPTION"
            elif max(r.import_kwh for r in recent) - min(r.import_kwh for r in recent) < D(
                "0.00001"
            ):
                kind = "FLATLINE"
        if kind:
            abnormal_meters.append({"meter_id": meter_id, "type": kind})
            meter_anomaly = s.scalar(
                select(Anomaly).where(
                    Anomaly.asset_id == meter_id,
                    Anomaly.start == start,
                    Anomaly.anomaly_type == kind,
                )
            )
            if not meter_anomaly:
                s.add(
                    Anomaly(
                        asset_id=meter_id,
                        start=start,
                        anomaly_type=kind,
                        severity="WATCH",
                        score=30,
                        evidence={"interval_id": interval.id, "history_samples": len(values)},
                    )
                )
    events = s.scalars(
        select(MeterEvent).where(
            MeterEvent.asset_id.in_(current.evidence["expected_meter_ids"]),
            MeterEvent.time >= start - timedelta(hours=1),
            MeterEvent.time < start + STEP,
        )
    ).all()
    persistence = D(sum(r.abnormal for r in slots)) / 6
    score = calculate_anomaly_score(
        (
            min(deviation / 20, D(1)),
            persistence,
            current.completeness / 100,
            D(len(abnormal_meters)) / max(current.evidence["valid_meter_count"], 1),
            min(D(len(events)) / 3, D(1)),
        )
    )
    confidence = current.completeness * min(D(len(history)) / 28, D(1))
    evidence.update(
        persistence_slots=sum(r.abnormal for r in slots),
        window_slots=6,
        abnormal_meters=abnormal_meters,
        relevant_event_ids=[e.id for e in events],
        confidence_score=str(confidence),
        score=str(score),
    )
    if prior:
        archive(s, "anomaly", asset_id, start, prior.evidence)
    else:
        prior = Anomaly(asset_id=asset_id, start=start, anomaly_type="ENERGY_IMBALANCE")
        s.add(prior)
    prior.status, prior.score, prior.evidence = "OPEN", score, evidence
    prior.severity = "CRITICAL" if score >= 80 else "HIGH" if score >= 60 else "WATCH"
    s.flush()
    # Group adjacent findings into a single open investigation, retaining original evidence.
    case = s.scalar(
        select(Case)
        .where(Case.asset_id == asset_id, Case.status.in_(["NEW", "ASSIGNED", "INVESTIGATING"]))
        .with_for_update()
    )
    if not case and not s.scalar(select(Case.id).where(Case.anomaly_id == prior.id)):
        case = Case(
            case_no=f"OGL-{uuid4().hex[:20]}",
            asset_id=asset_id,
            anomaly_id=prior.id,
            priority=score * confidence / 100,
            evidence=evidence,
        )
        s.add(case)
        s.flush()
        s.add(
            Audit(
                case_id=case.id,
                actor="anomaly-worker",
                body={"action": "CREATED", "anomaly_id": prior.id},
            )
        )
    emit(
        s,
        "anomaly",
        "openami.anomaly",
        asset_id,
        {"anomaly_id": prior.id, "start": start.isoformat()},
    )


def anomaly_handler(s, payload):
    asset_id, start = payload["asset_id"], datetime.fromisoformat(payload["start"])
    s.get(Asset, asset_id, with_for_update=True)
    # Recompute all later evaluations whose baseline/persistence can depend on this revision.
    affected = s.scalars(
        select(Balance.start)
        .where(
            Balance.asset_id == asset_id,
            Balance.start >= start,
            Balance.start <= start + timedelta(days=settings().baseline_days) + 5 * STEP,
        )
        .order_by(Balance.start)
    ).all()
    dependency_slots = {
        start + timedelta(days=day) + slot * STEP
        for day in range(settings().baseline_days + 1)
        for slot in range(6)
    }
    for time in affected:
        if time in dependency_slots:
            evaluate(s, asset_id, time)


def meter_evaluate(s, asset_id, start):
    interval = record(s, Interval, asset_id, start)
    if not interval:
        return
    history = s.scalars(
        select(Interval)
        .where(
            Interval.asset_id == asset_id,
            Interval.start >= start - timedelta(days=28),
            Interval.start < start,
            Interval.status.in_(VALID),
        )
        .order_by(Interval.start)
    ).all()
    comparable = [r for r in history if r.start.time() == start.time()]
    values = [r.import_kwh for r in comparable]
    kind = consumption_deviation(interval.import_kwh, values) if interval.status in VALID else None
    recent = history[-5:] + [interval]
    if (
        interval.status in VALID
        and len(recent) == 6
        and all(b.start - a.start == STEP for a, b in zip(recent, recent[1:], strict=False))
    ):
        if all(r.import_kwh == 0 for r in recent):
            kind = "ZERO_CONSUMPTION"
        elif max(r.import_kwh for r in recent) - min(r.import_kwh for r in recent) < D("0.00001"):
            kind = "FLATLINE"
    findings = s.scalars(
        select(Anomaly).where(Anomaly.asset_id == asset_id, Anomaly.start == start)
    ).all()
    for finding in findings:
        if finding.anomaly_type != kind:
            finding.status = "SUPERSEDED"
    if not kind:
        return
    recent_week = [r.import_kwh for r in comparable if r.start >= start - timedelta(days=7)]
    evidence = {
        "algorithm": "meter-v1",
        "interval_id": interval.id,
        "revision": interval.revision,
        "history_samples": len(values),
        "import_kwh": str(interval.import_kwh),
        "mean_28_days": str(sum(values, D(0)) / len(values)) if values else None,
        "mean_7_days": str(sum(recent_week, D(0)) / len(recent_week)) if recent_week else None,
        "type": kind,
    }
    finding = next((r for r in findings if r.anomaly_type == kind), None)
    if finding and finding.evidence == evidence and finding.status == "OPEN":
        return
    if finding:
        archive(s, "meter_anomaly", asset_id, start, finding.evidence)
    else:
        finding = Anomaly(
            asset_id=asset_id, start=start, anomaly_type=kind, score=30, severity="WATCH"
        )
        s.add(finding)
    finding.evidence, finding.status = evidence, "OPEN"
    s.flush()
    emit(
        s,
        "anomaly",
        "openami.anomaly",
        asset_id,
        {"anomaly_id": finding.id, "start": start.isoformat()},
    )


def meter_anomaly_handler(s, payload):
    asset_id, start = payload["asset_id"], datetime.fromisoformat(payload["start"])
    s.get(Asset, asset_id, with_for_update=True)
    meter_evaluate(s, asset_id, start)
    if payload.get("revision", 1) > 1:
        times = s.scalars(
            select(Interval.start).where(
                Interval.asset_id == asset_id,
                Interval.start > start,
                Interval.start <= start + timedelta(days=28) + 5 * STEP,
            )
        ).all()
        affected = {start + timedelta(days=d) + slot * STEP for d in range(29) for slot in range(6)}
        for time in sorted(times):
            if time in affected:
                meter_evaluate(s, asset_id, time)


def update_case(s, case_id, update, actor):
    case = s.get(Case, case_id, with_for_update=True)
    if not case:
        raise LookupError("case not found")
    if case.version != update.version:
        raise Conflict("case changed; reload before updating")
    transitions = {
        "NEW": {"ASSIGNED", "DISMISSED"},
        "ASSIGNED": {"INVESTIGATING", "DISMISSED"},
        "INVESTIGATING": {"RESOLVED", "DISMISSED"},
        "RESOLVED": {"NEW"},
        "DISMISSED": {"NEW"},
    }
    if update.status != case.status and update.status not in transitions[case.status]:
        raise ValueError("invalid case transition")
    if update.status == "ASSIGNED" and not update.assigned_to:
        raise ValueError("assignee required")
    if update.status in {"RESOLVED", "DISMISSED"} and not update.resolution:
        raise ValueError("resolution required")
    s.add(Audit(case_id=case.id, actor=actor, body={"from": case.status, **update.model_dump()}))
    case.status, case.assigned_to, case.resolution = (
        update.status,
        update.assigned_to,
        update.resolution,
    )
    case.closed_at = now() if case.status in {"RESOLVED", "DISMISSED"} else None
    case.version += 1
    return case
