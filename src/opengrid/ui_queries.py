"""Bounded read models for the operator application.

The browser consumes projections and recorded evidence, never a raw-reading dump.
Missing measurements and unavailable telemetry remain null. UI arithmetic formats
evidence; it does not change the accounting or anomaly algorithms.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import ceil
from statistics import mean

from sqlalchemy import Integer, String, and_, case, cast, func, literal, or_, select, union_all
from sqlalchemy.orm import aliased

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
    Feeder,
    Heartbeat,
    Interval,
    Job,
    MeterEvent,
    Outbox,
    Quarantine,
    Reading,
    now,
)

OPEN_CASES = ("NEW", "ASSIGNED", "INVESTIGATING")
VALID = ("VALID", "REGISTER_ROLLOVER")
PERIODS = {
    "24h": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}


def value(item):
    """Return JSON-safe values, retaining null instead of inventing observations."""
    if isinstance(item, Decimal):
        return float(item)
    if isinstance(item, datetime):
        return item.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")
    if isinstance(item, dict):
        return {key: value(val) for key, val in item.items()}
    if isinstance(item, (list, tuple)):
        return [value(val) for val in item]
    return item


def number(item):
    if item is None:
        return None
    try:
        result = float(item)
    except (TypeError, ValueError):
        return None
    return result if float("-inf") < result < float("inf") else None


def _date(item):
    if isinstance(item, str):
        item = datetime.fromisoformat(item.replace("Z", "+00:00"))
    if item.tzinfo:
        return item.astimezone(UTC).replace(tzinfo=None)
    return item


def time_window(period="7d", start=None, end=None):
    if period not in PERIODS:
        raise ValueError("period must be 24h, 7d, 30d, or 90d")
    end = _date(end) if end else now()
    start = _date(start) if start else end - PERIODS[period]
    if start >= end or end - start > timedelta(days=90):
        raise ValueError("time range must be positive and no longer than 90 days")
    return start, end


def _bounds(page, page_size):
    if not 1 <= page <= 10000 or not 1 <= page_size <= 100:
        raise ValueError("page must be 1–10000 and page_size must be 1–100")


def _page(s, query, columns, *, page=1, page_size=25, sort="id", order="asc", mapper=None):
    _bounds(page, page_size)
    if sort not in columns or order not in {"asc", "desc"}:
        raise ValueError("unsupported sort or order")
    total = s.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    column = columns[sort]
    query = query.order_by(column.desc() if order == "desc" else column.asc(), columns["id"].asc())
    rows = s.execute(query.offset((page - 1) * page_size).limit(page_size)).all()
    return {
        "items": [value(mapper(row) if mapper else dict(row._mapping)) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": ceil(total / page_size),
    }


def _latest(model, start, end, *conditions):
    return (
        select(model.id)
        .where(model.asset_id == Asset.id, model.start >= start, model.start < end, *conditions)
        .order_by(model.start.desc(), model.id.desc())
        .limit(1)
        .correlate(Asset)
        .scalar_subquery()
    )


def _assignment(at):
    return (
        select(Assignment.id)
        .where(
            Assignment.meter_id == Asset.id,
            Assignment.valid_from <= at,
            or_(Assignment.valid_to.is_(None), Assignment.valid_to > at),
        )
        .order_by(Assignment.valid_from.desc(), Assignment.id.desc())
        .limit(1)
        .correlate(Asset)
        .scalar_subquery()
    )


def _baseline(evidence):
    return number((evidence.get("baseline") or {}).get("median"))


def _finding(anomaly):
    return {
        "anomaly_id": anomaly.id if anomaly else None,
        "anomaly_type": anomaly.anomaly_type if anomaly else None,
        "anomaly_time": anomaly.start if anomaly else None,
        "score": number(anomaly.score) if anomaly else None,
        "severity": anomaly.severity if anomaly else "UNKNOWN",
        "confidence": number(anomaly.evidence.get("confidence_score")) if anomaly else None,
        "persistence": anomaly.evidence.get("persistence_slots") if anomaly else None,
    }


def _asset_row(asset, feeder):
    return {
        "id": asset.id,
        "code": asset.code,
        "name": asset.name,
        "active": asset.active,
        "feeder_id": feeder.id if feeder else None,
        "feeder_code": feeder.code if feeder else None,
    }


def _transformer_row(row):
    asset, feeder, balance, evaluation, anomaly, investigation, meters = row
    data = _asset_row(asset, feeder) | _finding(anomaly)
    data.update(
        meter_count=meters or 0,
        input_kwh=number(balance.input_kwh) if balance else None,
        downstream_kwh=number(balance.downstream_kwh) if balance and balance.completeness else None,
        unaccounted_kwh=number(balance.accounting_difference_kwh)
        if balance and balance.status in {"COMPLETE", "NON_POSITIVE_NET_INPUT"}
        else None,
        imbalance_percent=number(balance.accounting_difference_percent) if balance else None,
        completeness_percent=number(balance.completeness) if balance else None,
        baseline_percent=_baseline(evaluation.evidence) if evaluation else None,
        deviation_pp=number(evaluation.evidence.get("deviation_pp"))
        if evaluation and evaluation.eligible
        else None,
        status=balance.status if balance else "NO_DATA",
        last_interval=balance.start if balance else None,
        investigation_id=investigation.id if investigation else None,
        case_no=investigation.case_no if investigation else None,
        investigation_status=investigation.status if investigation else None,
        capacity_kva=None,
    )
    if not anomaly and balance:
        data["severity"] = "HEALTHY" if balance.status == "COMPLETE" else "UNKNOWN"
    return data


def list_transformers(
    s,
    *,
    page=1,
    page_size=25,
    q="",
    sort="code",
    order="asc",
    feeder=None,
    severity=None,
    investigation_status=None,
    min_imbalance=None,
    max_imbalance=None,
    min_completeness=None,
    max_completeness=None,
    asset_id=None,
    period="7d",
    start=None,
    end=None,
):
    start, end = time_window(period, start, end)
    latest_case = (
        select(Case.id)
        .where(Case.asset_id == Asset.id)
        .order_by(Case.id.desc())
        .limit(1)
        .correlate(Asset)
        .scalar_subquery()
    )
    meters = (
        select(func.count(func.distinct(Assignment.meter_id)))
        .where(
            Assignment.transformer_id == Asset.id,
            Assignment.valid_from < end,
            or_(Assignment.valid_to.is_(None), Assignment.valid_to >= end),
        )
        .correlate(Asset)
        .scalar_subquery()
    )
    severity_col = case(
        (Anomaly.id.is_not(None), Anomaly.severity),
        (Balance.status == "COMPLETE", "HEALTHY"),
        else_="UNKNOWN",
    )
    query = (
        select(Asset, Feeder, Balance, Evaluation, Anomaly, Case, meters)
        .outerjoin(Feeder, Feeder.id == Asset.feeder_id)
        .outerjoin(
            Balance, and_(Balance.asset_id == Asset.id, Balance.id == _latest(Balance, start, end))
        )
        .outerjoin(
            Evaluation, and_(Evaluation.asset_id == Asset.id, Evaluation.start == Balance.start)
        )
        .outerjoin(
            Anomaly,
            and_(
                Anomaly.asset_id == Asset.id,
                Anomaly.id == _latest(Anomaly, start, end, Anomaly.status == "OPEN"),
            ),
        )
        .outerjoin(Case, and_(Case.asset_id == Asset.id, Case.id == latest_case))
        .where(Asset.kind == "TRANSFORMER")
    )
    if q:
        query = query.where(
            or_(
                Asset.code.contains(q[:128], autoescape=True),
                Asset.name.contains(q[:128], autoescape=True),
            )
        )
    for condition, expression in (
        (asset_id, Asset.id == asset_id),
        (feeder, Feeder.code == feeder),
        (severity, severity_col == severity),
        (investigation_status, Case.status == investigation_status),
    ):
        if condition is not None and condition != "":
            query = query.where(expression)
    for threshold, expression in (
        (
            min_imbalance,
            Balance.accounting_difference_percent >= min_imbalance
            if min_imbalance is not None
            else None,
        ),
        (
            max_imbalance,
            Balance.accounting_difference_percent <= max_imbalance
            if max_imbalance is not None
            else None,
        ),
        (
            min_completeness,
            Balance.completeness >= min_completeness if min_completeness is not None else None,
        ),
        (
            max_completeness,
            Balance.completeness <= max_completeness if max_completeness is not None else None,
        ),
    ):
        if threshold is not None:
            query = query.where(expression)
    sorts = {
        "id": Asset.id,
        "code": Asset.code,
        "name": Asset.name,
        "feeder": Feeder.code,
        "meter_count": meters,
        "input_kwh": Balance.input_kwh,
        "downstream_kwh": Balance.downstream_kwh,
        "unaccounted_kwh": Balance.accounting_difference_kwh,
        "imbalance_percent": Balance.accounting_difference_percent,
        "completeness_percent": Balance.completeness,
        "score": Anomaly.score,
        "severity": severity_col,
        "last_interval": Balance.start,
        "baseline_percent": Evaluation.evidence["baseline"]["median"].as_float(),
    }
    return _page(
        s,
        query,
        sorts,
        page=page,
        page_size=page_size,
        sort=sort,
        order=order,
        mapper=_transformer_row,
    )


def _meter_row(row):
    asset, transformer, feeder, interval, anomaly, last_reading = row
    data = _asset_row(asset, feeder) | _finding(anomaly)
    evidence = anomaly.evidence if anomaly else {}
    current = number(interval.import_kwh) if interval and interval.status in VALID else None
    baseline = (
        number(evidence.get("mean_28_days"))
        if anomaly and interval and anomaly.start == interval.start
        else None
    )
    data.update(
        transformer_id=transformer.id if transformer else None,
        transformer_code=transformer.code if transformer else None,
        current_kwh=current,
        baseline_7d=number(evidence.get("mean_7_days")) if baseline is not None else None,
        baseline_28d=baseline,
        deviation_percent=(current - baseline) / baseline * 100
        if current is not None and baseline
        else None,
        completeness_percent=None,
        last_reading=last_reading,
        last_interval=interval.start if interval else None,
        status=interval.status if interval else "NO_DATA",
        consumer_type=None,
        phase=None,
        sanctioned_load_kw=None,
    )
    if not anomaly and interval:
        data["severity"] = "HEALTHY" if interval.status in VALID else "UNKNOWN"
    return data


def list_meters(
    s,
    *,
    page=1,
    page_size=25,
    q="",
    sort="code",
    order="asc",
    transformer_id=None,
    feeder=None,
    status=None,
    anomaly=None,
    asset_id=None,
    asset_ids=None,
    period="7d",
    start=None,
    end=None,
):
    start, end = time_window(period, start, end)
    transformer = aliased(Asset)
    last_reading = (
        select(Reading.time)
        .where(Reading.asset_id == Asset.id, Reading.time >= start, Reading.time < end)
        .order_by(Reading.time.desc(), Reading.id.desc())
        .limit(1)
        .correlate(Asset)
        .scalar_subquery()
    )
    query = (
        select(Asset, transformer, Feeder, Interval, Anomaly, last_reading)
        .outerjoin(
            Assignment,
            and_(
                Assignment.meter_id == Asset.id,
                Assignment.id == _assignment(end - timedelta(microseconds=1)),
            ),
        )
        .outerjoin(transformer, transformer.id == Assignment.transformer_id)
        .outerjoin(Feeder, Feeder.id == transformer.feeder_id)
        .outerjoin(
            Interval,
            and_(Interval.asset_id == Asset.id, Interval.id == _latest(Interval, start, end)),
        )
        .outerjoin(
            Anomaly,
            and_(
                Anomaly.asset_id == Asset.id,
                Anomaly.id == _latest(Anomaly, start, end, Anomaly.status == "OPEN"),
            ),
        )
        .where(Asset.kind == "METER")
    )
    if q:
        query = query.where(
            or_(
                Asset.code.contains(q[:128], autoescape=True),
                Asset.name.contains(q[:128], autoescape=True),
            )
        )
    for condition, expression in (
        (transformer_id, transformer.id == transformer_id),
        (feeder, Feeder.code == feeder),
        (status, Interval.status == status),
        (anomaly, Anomaly.anomaly_type == anomaly),
        (asset_id, Asset.id == asset_id),
    ):
        if condition is not None and condition != "":
            query = query.where(expression)
    if asset_ids is not None:
        if len(asset_ids) > 1000:
            raise ValueError("at most 1000 meter identifiers may be selected")
        query = query.where(Asset.id.in_(asset_ids))
    sorts = {
        "id": Asset.id,
        "code": Asset.code,
        "name": Asset.name,
        "transformer_code": transformer.code,
        "current_kwh": Interval.import_kwh,
        "status": Interval.status,
        "score": Anomaly.score,
        "last_reading": last_reading,
        "last_interval": Interval.start,
        "baseline_28d": Anomaly.evidence["mean_28_days"].as_float(),
    }
    return _page(
        s, query, sorts, page=page, page_size=page_size, sort=sort, order=order, mapper=_meter_row
    )


def _case_row(row):
    investigation, asset, transformer, feeder, anomaly = row
    evidence = investigation.evidence or {}
    return {
        "id": investigation.id,
        "case_no": investigation.case_no,
        "asset_id": asset.id,
        "asset_code": asset.code,
        "asset_kind": asset.kind,
        "transformer_id": asset.id
        if asset.kind == "TRANSFORMER"
        else transformer.id
        if transformer
        else None,
        "transformer_code": asset.code
        if asset.kind == "TRANSFORMER"
        else transformer.code
        if transformer
        else None,
        "feeder_code": feeder.code if feeder else None,
        "status": investigation.status,
        "priority": number(investigation.priority),
        "severity": anomaly.severity,
        "anomaly_id": anomaly.id,
        "anomaly_type": anomaly.anomaly_type,
        "unaccounted_kwh": number(evidence.get("accounting_difference_kwh")),
        "imbalance_percent": number(evidence.get("accounting_difference_percent")),
        "baseline_percent": _baseline(evidence),
        "confidence": number(evidence.get("confidence_score")),
        "assigned_to": investigation.assigned_to,
        "resolution": investigation.resolution,
        "version": investigation.version,
        "opened_at": investigation.opened_at,
        "closed_at": investigation.closed_at,
    }


def list_investigations(
    s,
    *,
    page=1,
    page_size=25,
    q="",
    sort="priority",
    order="desc",
    status=None,
    severity=None,
    assigned_to=None,
    transformer_id=None,
    feeder=None,
    min_score=None,
    case_id=None,
    start=None,
    end=None,
):
    transformer = aliased(Asset)
    query = (
        select(Case, Asset, transformer, Feeder, Anomaly)
        .join(Asset, Asset.id == Case.asset_id)
        .join(Anomaly, Anomaly.id == Case.anomaly_id)
        .outerjoin(
            Assignment, and_(Assignment.meter_id == Asset.id, Assignment.id == _assignment(now()))
        )
        .outerjoin(transformer, transformer.id == Assignment.transformer_id)
        .outerjoin(Feeder, Feeder.id == func.coalesce(Asset.feeder_id, transformer.feeder_id))
    )
    if q:
        query = query.where(
            or_(
                Case.case_no.contains(q[:128], autoescape=True),
                Asset.code.contains(q[:128], autoescape=True),
            )
        )
    for condition, expression in (
        (status, Case.status.in_(OPEN_CASES) if status == "OPEN" else Case.status == status),
        (severity, Anomaly.severity == severity),
        (assigned_to, Case.assigned_to == assigned_to),
        (feeder, Feeder.code == feeder),
        (case_id, Case.id == case_id),
    ):
        if condition is not None and condition != "":
            query = query.where(expression)
    if transformer_id is not None:
        query = query.where(
            or_(
                and_(Asset.kind == "TRANSFORMER", Asset.id == transformer_id),
                transformer.id == transformer_id,
            )
        )
    if min_score is not None:
        query = query.where(Case.priority >= min_score)
    if start is not None or end is not None:
        start, end = time_window("30d", start, end)
        query = query.where(Case.opened_at >= start, Case.opened_at < end)
    sorts = {
        "id": Case.id,
        "case_no": Case.case_no,
        "asset_code": Asset.code,
        "priority": Case.priority,
        "status": Case.status,
        "severity": Anomaly.severity,
        "assigned_to": Case.assigned_to,
        "opened_at": Case.opened_at,
        "confidence": Case.evidence["confidence_score"].as_float(),
    }
    return _page(
        s, query, sorts, page=page, page_size=page_size, sort=sort, order=order, mapper=_case_row
    )


def _anomaly_row(row):
    anomaly, asset = row
    return {
        "id": anomaly.id,
        "asset_id": asset.id,
        "asset_code": asset.code,
        "asset_kind": asset.kind,
        "start": anomaly.start,
        "anomaly_type": anomaly.anomaly_type,
        "status": anomaly.status,
        "severity": anomaly.severity,
        "score": number(anomaly.score),
        "confidence": number(anomaly.evidence.get("confidence_score")),
        "imbalance_percent": number(anomaly.evidence.get("accounting_difference_percent")),
        "baseline_percent": _baseline(anomaly.evidence),
        "unaccounted_kwh": number(anomaly.evidence.get("accounting_difference_kwh")),
    }


def list_anomalies(
    s,
    *,
    page=1,
    page_size=25,
    q="",
    sort="start",
    order="desc",
    asset_id=None,
    asset_kind=None,
    status=None,
    severity=None,
    anomaly_type=None,
    period="7d",
    start=None,
    end=None,
):
    start, end = time_window(period, start, end)
    query = (
        select(Anomaly, Asset)
        .join(Asset, Asset.id == Anomaly.asset_id)
        .where(Anomaly.start >= start, Anomaly.start < end)
    )
    if q:
        query = query.where(Asset.code.contains(q[:128], autoescape=True))
    for condition, expression in (
        (asset_id, Asset.id == asset_id),
        (asset_kind, Asset.kind == asset_kind),
        (status, Anomaly.status == status),
        (severity, Anomaly.severity == severity),
        (anomaly_type, Anomaly.anomaly_type == anomaly_type),
    ):
        if condition is not None and condition != "":
            query = query.where(expression)
    return _page(
        s,
        query,
        {
            "id": Anomaly.id,
            "start": Anomaly.start,
            "score": Anomaly.score,
            "severity": Anomaly.severity,
            "asset_code": Asset.code,
            "anomaly_type": Anomaly.anomaly_type,
        },
        page=page,
        page_size=page_size,
        sort=sort,
        order=order,
        mapper=_anomaly_row,
    )


def dashboard_summary(s, *, period="7d", start=None, end=None):
    start, end = time_window(period, start, end)
    assets = dict(s.execute(select(Asset.kind, func.count()).group_by(Asset.kind)).all())
    totals = s.execute(
        select(
            func.sum(Balance.input_kwh),
            func.sum(Balance.downstream_kwh),
            func.sum(Balance.accounting_difference_kwh),
            func.count(),
        ).where(Balance.start >= start, Balance.start < end, Balance.status == "COMPLETE")
    ).one()
    completeness = s.execute(
        select(
            func.sum(Aggregate.expected), func.sum(Aggregate.valid), func.sum(Aggregate.received)
        ).where(Aggregate.start >= start, Aggregate.start < end)
    ).one()
    open_cases = (
        s.scalar(select(func.count()).select_from(Case).where(Case.status.in_(OPEN_CASES))) or 0
    )
    latest_anomaly = _latest(Anomaly, start, end, Anomaly.status == "OPEN")
    health = dict(
        s.execute(
            select(
                case(
                    (Anomaly.id.is_not(None), Anomaly.severity),
                    (Balance.status == "COMPLETE", "HEALTHY"),
                    else_="UNKNOWN",
                ),
                func.count(),
            )
            .select_from(Asset)
            .outerjoin(
                Balance,
                and_(Balance.asset_id == Asset.id, Balance.id == _latest(Balance, start, end)),
            )
            .outerjoin(Anomaly, and_(Anomaly.asset_id == Asset.id, Anomaly.id == latest_anomaly))
            .where(Asset.kind == "TRANSFORMER")
            .group_by(
                case(
                    (Anomaly.id.is_not(None), Anomaly.severity),
                    (Balance.status == "COMPLETE", "HEALTHY"),
                    else_="UNKNOWN",
                )
            )
        ).all()
    )
    quality = quality_summary(s, start=start, end=end)
    return value(
        {
            "transformers": assets.get("TRANSFORMER", 0),
            "meters": assets.get("METER", 0),
            "input_kwh": number(totals[0]),
            "downstream_kwh": number(totals[1]),
            "unaccounted_kwh": number(totals[2]),
            "average_imbalance_percent": float(totals[2] / totals[0] * 100) if totals[0] else None,
            "data_completeness_percent": float(completeness[1] / completeness[0] * 100)
            if completeness[0]
            else None,
            "open_investigations": open_cases,
            "critical_anomalies": health.get("CRITICAL", 0),
            "high_anomalies": health.get("HIGH", 0),
            "network_health": {
                key: health.get(key, 0)
                for key in ("HEALTHY", "WATCH", "HIGH", "CRITICAL", "UNKNOWN")
            },
            "complete_intervals": totals[3],
            "expected_meter_intervals": completeness[0],
            "received_meter_intervals": completeness[2],
            "quality": quality,
            "start": start,
            "end": end,
            "energy_basis": "Complete transformer intervals only; network imbalance is energy-weighted.",
            "completeness_basis": "Valid / expected meter intervals in calculated transformer aggregates.",
        }
    )


def _bucket(s, column, start, end):
    duration = (end - start).total_seconds()
    seconds = (
        900
        if duration <= 86400
        else 3600
        if duration <= 604800
        else 21600
        if duration <= 2592000
        else 43200
    )
    epoch = (
        cast(func.strftime("%s", column), Integer)
        if s.get_bind().dialect.name == "sqlite"
        else func.unix_timestamp(column)
    )
    return cast(epoch / seconds, Integer) if s.get_bind().dialect.name == "sqlite" else func.floor(
        epoch / seconds
    ), seconds


def dashboard_trends(s, *, period="7d", transformer_id=None, start=None, end=None):
    start, end = time_window(period, start, end)
    bucket, seconds = _bucket(s, Balance.start, start, end)
    complete = Balance.status == "COMPLETE"
    query = (
        select(
            bucket.label("bucket"),
            func.sum(case((complete, Balance.input_kwh))),
            func.sum(case((complete, Balance.downstream_kwh))),
            func.sum(case((complete, Balance.accounting_difference_kwh))),
            func.sum(Aggregate.expected),
            func.sum(Aggregate.valid),
            func.count(),
            func.sum(case((complete, 1), else_=0)),
            func.avg(Evaluation.evidence["baseline"]["median"].as_float()),
        )
        .select_from(Balance)
        .outerjoin(
            Aggregate,
            and_(Aggregate.asset_id == Balance.asset_id, Aggregate.start == Balance.start),
        )
        .outerjoin(
            Evaluation,
            and_(Evaluation.asset_id == Balance.asset_id, Evaluation.start == Balance.start),
        )
        .where(Balance.start >= start, Balance.start < end)
    )
    if transformer_id is not None:
        query = query.where(Balance.asset_id == transformer_id)
    rows = s.execute(query.group_by(bucket).order_by(bucket).limit(400)).all()
    points = [
        {
            "time": datetime.fromtimestamp(int(r[0]) * seconds, UTC).replace(tzinfo=None),
            "input_kwh": number(r[1]),
            "downstream_kwh": number(r[2]),
            "unaccounted_kwh": number(r[3]),
            "imbalance_percent": float(r[3] / r[1] * 100) if r[1] else None,
            "completeness_percent": float(r[5] / r[4] * 100) if r[4] else None,
            "sample_count": r[6],
            "complete_intervals": r[7],
            "baseline_percent": number(r[8]) if transformer_id is not None else None,
        }
        for r in rows
    ]
    return value(
        {
            "points": points,
            "start": start,
            "end": end,
            "bucket_minutes": seconds // 60,
            "energy_basis": "Complete transformer intervals only",
        }
    )


def meter_trends(s, asset_id, *, period="7d", start=None, end=None):
    start, end = time_window(period, start, end)
    bucket, seconds = _bucket(s, Interval.start, start, end)
    rows = s.execute(
        select(
            bucket,
            func.sum(case((Interval.status.in_(VALID), Interval.import_kwh))),
            func.count(),
            func.sum(case((Interval.status.in_(VALID), 1), else_=0)),
        )
        .where(Interval.asset_id == asset_id, Interval.start >= start, Interval.start < end)
        .group_by(bucket)
        .order_by(bucket)
        .limit(400)
    ).all()
    return value(
        {
            "points": [
                {
                    "time": datetime.fromtimestamp(int(r[0]) * seconds, UTC).replace(tzinfo=None),
                    "current_kwh": number(r[1]),
                    "actual_kwh": number(r[1]),
                    "import_kwh": number(r[1]),
                    "baseline_7d": None,
                    "baseline_28d": None,
                    "completeness_percent": r[3] / r[2] * 100 if r[2] else None,
                }
                for r in rows
            ],
            "start": start,
            "end": end,
            "bucket_minutes": seconds // 60,
            "baseline_note": "Same-slot baselines are available in current evidence; a continuous baseline series is not stored.",
        }
    )


def quality_summary(s, *, period="7d", start=None, end=None, asset_id=None):
    start, end = time_window(period, start, end)
    flags = cast(Interval.evidence["flags"], String)
    query = select(
        func.count(),
        func.sum(case((Interval.status.in_(VALID), 1), else_=0)),
        func.sum(case((Interval.status == "MISSING", 1), else_=0)),
        func.sum(case((flags.contains('"LATE"'), 1), else_=0)),
        func.sum(case((flags.contains('"OUT_OF_ORDER"'), 1), else_=0)),
        func.sum(case((Interval.status.not_in((*VALID, "MISSING")), 1), else_=0)),
    )
    query = query.where(Interval.start >= start, Interval.start < end)
    if asset_id is not None:
        query = query.where(Interval.asset_id == asset_id)
    total, valid, missing, late, out_of_order, invalid = s.execute(query).one()
    communication = (
        select(func.count())
        .select_from(MeterEvent)
        .where(
            MeterEvent.time >= start,
            MeterEvent.time < end,
            MeterEvent.event_type == "COMMUNICATION_FAILURE",
        )
    )
    if asset_id is not None:
        communication = communication.where(MeterEvent.asset_id == asset_id)
    expected = None
    if asset_id is not None:
        configurations = s.execute(
            select(Configuration.valid_from, Configuration.valid_to)
            .where(
                Configuration.asset_id == asset_id,
                Configuration.valid_from < end,
                or_(Configuration.valid_to.is_(None), Configuration.valid_to > start),
            )
            .order_by(Configuration.valid_from)
            .limit(100)
        ).all()
        if configurations:
            # Merge overlapping configuration spans, then count fully covered quarter-hour slots.
            spans = []
            for left, right in configurations:
                left, right = max(left, start), min(right or end, end)
                if spans and left <= spans[-1][1]:
                    spans[-1][1] = max(spans[-1][1], right)
                else:
                    spans.append([left, right])
            expected = sum(
                max(0, int(right.timestamp() // 900 - ceil(left.timestamp() / 900)))
                for left, right in spans
            )
    return value(
        {
            "projected_intervals": total,
            "expected_intervals": expected,
            "received": total - (missing or 0),
            "valid": valid or 0,
            "missing": max(0, expected - (total - (missing or 0)))
            if expected is not None
            else missing or 0,
            "late": late or 0,
            "invalid": invalid or 0,
            "out_of_order": out_of_order or 0,
            "duplicates": None,
            "communication_failures": s.scalar(communication) or 0,
            "completeness_percent": (valid or 0) / expected * 100
            if expected
            else (valid or 0) / total * 100
            if total and expected is None
            else None,
            "basis": "Configured schedule"
            if expected is not None
            else "Projected intervals only; unprocessed intervals are unknown",
            "duplicates_note": "Duplicate receipts are idempotent; this MVP does not persist a duplicate counter.",
            "start": start,
            "end": end,
        }
    )


def list_quality(
    s,
    *,
    page=1,
    page_size=25,
    q="",
    sort="start",
    order="desc",
    status=None,
    transformer_id=None,
    meter_id=None,
    period="7d",
    start=None,
    end=None,
):
    start, end = time_window(period, start, end)
    query = (
        select(Interval, Asset)
        .join(Asset, Asset.id == Interval.asset_id)
        .where(Interval.start >= start, Interval.start < end)
    )
    if q:
        query = query.where(Asset.code.contains(q[:128], autoescape=True))
    if status:
        if status in {"LATE", "OUT_OF_ORDER"}:
            query = query.where(cast(Interval.evidence["flags"], String).contains(f'"{status}"'))
        else:
            query = query.where(Interval.status == status)
    if meter_id is not None:
        query = query.where(Asset.id == meter_id)
    if transformer_id is not None:
        members = select(Assignment.meter_id).where(
            Assignment.transformer_id == transformer_id,
            Assignment.valid_from <= Interval.start,
            or_(Assignment.valid_to.is_(None), Assignment.valid_to > Interval.start),
        )
        query = query.where(or_(Asset.id == transformer_id, Asset.id.in_(members)))
    return _page(
        s,
        query,
        {
            "id": Interval.id,
            "start": Interval.start,
            "asset_code": Asset.code,
            "status": Interval.status,
            "import_kwh": Interval.import_kwh,
        },
        page=page,
        page_size=page_size,
        sort=sort,
        order=order,
        mapper=lambda row: {
            "id": row[0].id,
            "asset_id": row[1].id,
            "asset_code": row[1].code,
            "asset_kind": row[1].kind,
            "start": row[0].start,
            "status": row[0].status,
            "import_kwh": number(row[0].import_kwh),
            "revision": row[0].revision,
            "flags": row[0].evidence.get("flags", []),
        },
    )


def evidence_explanation(evidence):
    """Only describe facts present in the immutable finding/case snapshot."""
    lines = []
    current, baseline = number(evidence.get("accounting_difference_percent")), _baseline(evidence)
    if current is not None:
        lines.append(f"Accounting imbalance was {current:.1f}% at the flagged interval.")
    if baseline is not None:
        lines.append(f"The same-slot historical median was {baseline:.1f}%.")
    if current is not None and baseline is not None:
        lines.append(f"The deviation was {current - baseline:+.1f} percentage points.")
    if evidence.get("persistence_slots") is not None:
        lines.append(
            f"{evidence['persistence_slots']} of {evidence.get('window_slots', 6)} consecutive intervals exceeded the threshold."
        )
    expected, valid = evidence.get("expected_meter_count"), evidence.get("valid_meter_count")
    if expected is not None and valid is not None:
        lines.append(f"{valid} of {expected} expected meters supplied valid interval energy.")
    if evidence.get("historical_samples") is not None:
        lines.append(
            f"The baseline used {evidence['historical_samples']} comparable historical samples."
        )
    if "abnormal_meters" in evidence:
        lines.append(
            f"{len(evidence['abnormal_meters'])} downstream meters had correlated consumption anomalies."
        )
    if "relevant_event_ids" in evidence:
        lines.append(
            f"{len(evidence['relevant_event_ids'])} meter events occurred in the correlation window."
        )
    if evidence.get("suppression_reason"):
        lines.append(
            f"Evaluation was suppressed: {evidence['suppression_reason'].replace('_', ' ').lower()}."
        )
    if evidence.get("type"):
        lines.append(f"Recorded meter finding: {evidence['type'].replace('_', ' ').lower()}.")
    if evidence.get("history_samples") is not None:
        lines.append(f"Meter history contained {evidence['history_samples']} comparable samples.")
    return lines or ["No detailed evaluation evidence is available for this observation."]


def score_components(evidence):
    if evidence.get("algorithm") != "anomaly-v1" or "score" not in evidence:
        return []
    expected, valid = evidence.get("expected_meter_count"), evidence.get("valid_meter_count")
    components = [
        ("Loss deviation", min((number(evidence.get("deviation_pp")) or 0) / 20, 1), 35),
        ("Persistence", evidence.get("persistence_slots", 0) / 6, 25),
        ("Data completeness", valid / expected if expected and valid is not None else None, 20),
        ("Consumer anomalies", len(evidence.get("abnormal_meters", [])) / max(valid or 0, 1), 10),
        ("Event correlation", min(len(evidence.get("relevant_event_ids", [])) / 3, 1), 10),
    ]
    return [
        {
            "name": name,
            "value": component * 100 if component is not None else None,
            "weight": weight,
            "contribution": component * weight if component is not None else None,
        }
        for name, component, weight in components
    ]


def _event_queries(start, end, asset_id=None, transformer_id=None):
    def scope(query, column, time):
        query = query.where(time >= start, time < end)
        if asset_id is not None:
            query = query.where(column == asset_id)
        if transformer_id is not None:
            membership = select(Assignment.meter_id).where(
                Assignment.transformer_id == transformer_id,
                Assignment.valid_from <= time,
                or_(Assignment.valid_to.is_(None), Assignment.valid_to > time),
            )
            query = query.where(or_(column == transformer_id, column.in_(membership)))
        return query

    events = select(
        cast(MeterEvent.id, String).label("id"),
        MeterEvent.time.label("time"),
        Asset.id.label("asset_id"),
        Asset.code.label("asset_code"),
        Asset.kind.label("asset_kind"),
        MeterEvent.event_type.label("event_type"),
        literal("meter-event").label("source"),
        literal("INFO").label("severity"),
        MeterEvent.evidence.label("details"),
        literal(None, Integer).label("case_id"),
    )
    events = scope(events.join(Asset, Asset.id == MeterEvent.asset_id), Asset.id, MeterEvent.time)
    anomalies = select(
        cast(Anomaly.id, String),
        Anomaly.start,
        Asset.id,
        Asset.code,
        Asset.kind,
        Anomaly.anomaly_type,
        literal("anomaly"),
        Anomaly.severity,
        Anomaly.evidence,
        literal(None, Integer),
    )
    anomalies = scope(anomalies.join(Asset, Asset.id == Anomaly.asset_id), Asset.id, Anomaly.start)
    audits = select(
        cast(Audit.id, String),
        Audit.time,
        Asset.id,
        Asset.code,
        Asset.kind,
        literal("INVESTIGATION"),
        literal("case-history"),
        literal("INFO"),
        Audit.body,
        Case.id,
    )
    audits = scope(
        audits.join(Case, Case.id == Audit.case_id).join(Asset, Asset.id == Case.asset_id),
        Asset.id,
        Audit.time,
    )
    intervals = select(
        cast(Interval.id, String),
        Interval.start,
        Asset.id,
        Asset.code,
        Asset.kind,
        case((Interval.status.in_(VALID), "READING"), else_="QUALITY"),
        literal("normalized-interval"),
        case((Interval.status.in_(VALID), "INFO"), else_="WATCH"),
        Interval.evidence,
        literal(None, Integer),
    )
    intervals = scope(
        intervals.join(Asset, Asset.id == Interval.asset_id), Asset.id, Interval.start
    )
    return events, anomalies, audits, intervals


def list_events(
    s,
    *,
    page=1,
    page_size=25,
    q="",
    sort="time",
    order="desc",
    asset_id=None,
    meter_id=None,
    transformer_id=None,
    event_type=None,
    severity=None,
    period="24h",
    start=None,
    end=None,
):
    start, end = time_window(period, start, end)
    combined = union_all(
        *_event_queries(start, end, asset_id or meter_id, transformer_id)
    ).subquery()
    query = select(combined)
    if q:
        query = query.where(combined.c.asset_code.contains(q[:128], autoescape=True))
    if event_type:
        if event_type == "ANOMALY":
            query = query.where(combined.c.source == "anomaly")
        elif event_type == "COMMUNICATION":
            query = query.where(
                combined.c.event_type.in_(("COMMUNICATION_FAILURE", "COMMUNICATION_RESTORED"))
            )
        else:
            query = query.where(combined.c.event_type == event_type)
    if severity:
        query = query.where(combined.c.severity == severity)

    def mapper(row):
        result = dict(row._mapping)
        result["entity_code"] = result["asset_code"]
        result["url"] = (
            f"/investigations/{result['case_id']}"
            if result["case_id"]
            else f"/{'meters' if result['asset_kind'] == 'METER' else 'transformers'}/{result['asset_id']}"
        )
        return result

    return _page(
        s,
        query,
        {
            "id": func.concat(combined.c.source, ":", combined.c.id),
            "time": combined.c.time,
            "asset_code": combined.c.asset_code,
            "event_type": combined.c.event_type,
            "severity": combined.c.severity,
        },
        page=page,
        page_size=page_size,
        sort=sort,
        order=order,
        mapper=mapper,
    )


def entity_timeline(s, asset_id, *, include_meters=False, page=1, page_size=25, q="",
                    sort="time", order="desc", event_type=None, severity=None,
                    period="24h", start=None, end=None):
    return list_events(
        s,
        transformer_id=asset_id if include_meters else None,
        asset_id=None if include_meters else asset_id,
        page=page, page_size=page_size, q=q, sort=sort, order=order,
        event_type=event_type, severity=severity, period=period, start=start, end=end,
    )


def investigation_timeline(s, case_id, *, page=1, page_size=50, order="desc"):
    query = select(
        Audit.id, Audit.time, Audit.actor, Audit.body.label("details"), Audit.case_id
    ).where(Audit.case_id == case_id)
    return _page(
        s,
        query,
        {"id": Audit.id, "time": Audit.time},
        page=page,
        page_size=page_size,
        sort="time",
        order=order,
    )


def transformer_detail(s, asset_id, *, period="7d", start=None, end=None):
    window = {"period": period, "start": start, "end": end}
    rows = list_transformers(s, asset_id=asset_id, **window)["items"]
    if not rows:
        return None
    result = rows[0]
    finding = s.get(Anomaly, result["anomaly_id"]) if result["anomaly_id"] else None
    evidence = finding.evidence if finding else {}
    if not finding and result["last_interval"]:
        evaluation = s.scalar(
            select(Evaluation)
            .where(
                Evaluation.asset_id == asset_id, Evaluation.start == _date(result["last_interval"])
            )
            .limit(1)
        )
        evidence = evaluation.evidence if evaluation else {}
    result.update(
        evidence=evidence,
        explanation=evidence_explanation(evidence),
        score_components=score_components(evidence),
        trend=dashboard_trends(s, transformer_id=asset_id, **window),
        meters=list_meters(s, transformer_id=asset_id, **window),
        anomalies=list_anomalies(s, asset_id=asset_id, **window),
        timeline=entity_timeline(s, asset_id, include_meters=True, **window),
        flagged_imbalance_percent=number(evidence.get("accounting_difference_percent")),
        flagged_baseline_percent=_baseline(evidence),
        flagged_deviation_pp=number(evidence.get("deviation_pp")),
    )
    affected = [
        item["meter_id"] for item in evidence.get("abnormal_meters", []) if "meter_id" in item
    ]
    result["affected_meters"] = list_meters(s, asset_ids=affected[:1000], **window)
    return value(result)


def meter_detail(s, asset_id, *, period="7d", start=None, end=None):
    window = {"period": period, "start": start, "end": end}
    rows = list_meters(s, asset_id=asset_id, **window)["items"]
    if not rows:
        return None
    result = rows[0]
    finding = s.get(Anomaly, result["anomaly_id"]) if result["anomaly_id"] else None
    evidence = finding.evidence if finding else {}
    if result["last_interval"]:
        slot = _date(result["last_interval"])
        comparable_times = [slot - timedelta(days=day) for day in range(1, 29)]
        history = s.execute(
            select(Interval.start, Interval.import_kwh)
            .where(
                Interval.asset_id == asset_id,
                Interval.start.in_(comparable_times),
                Interval.status.in_(VALID),
                Interval.import_kwh.is_not(None),
            )
            .order_by(Interval.start)
            .limit(28)
        ).all()
        result["baseline_28d"] = (
            mean(float(row[1]) for row in history) if len(history) >= 7 else None
        )
        week = [float(row[1]) for row in history if row[0] >= slot - timedelta(days=7)]
        result["baseline_7d"] = mean(week) if len(week) >= 7 else None
        baseline = result["baseline_28d"]
        result["deviation_percent"] = (
            (result["current_kwh"] - baseline) / baseline * 100
            if baseline and result["current_kwh"] is not None
            else None
        )
    result.update(
        evidence=evidence,
        explanation=evidence_explanation(evidence),
        quality=quality_summary(s, asset_id=asset_id, **window),
        trend=meter_trends(s, asset_id, **window),
        anomalies=list_anomalies(s, asset_id=asset_id, **window),
        timeline=entity_timeline(s, asset_id, **window),
        voltage=None,
        current=None,
        frequency=None,
    )
    result["completeness_percent"] = result["quality"]["completeness_percent"]
    return value(result)


def investigation_detail(s, case_id, *, period="7d", start=None, end=None):
    rows = list_investigations(s, case_id=case_id)["items"]
    if not rows:
        return None
    result = rows[0]
    investigation = s.get(Case, case_id)
    evidence = investigation.evidence or {}
    result.update(
        evidence=evidence,
        explanation=evidence_explanation(evidence),
        score_components=score_components(evidence),
        timeline=investigation_timeline(s, case_id),
        anomaly_time=value(s.get(Anomaly, investigation.anomaly_id).start),
    )
    window = {"period": period, "start": start, "end": end}
    result["trend"] = (
        dashboard_trends(s, transformer_id=result["asset_id"], **window)
        if result["asset_kind"] == "TRANSFORMER"
        else meter_trends(s, result["asset_id"], **window)
    )
    result["events"] = entity_timeline(
        s, result["asset_id"], include_meters=result["asset_kind"] == "TRANSFORMER", **window
    )
    affected = [
        item["meter_id"] for item in evidence.get("abnormal_meters", []) if "meter_id" in item
    ]
    result["affected_meters"] = list_meters(s, asset_ids=affected[:1000], **window)
    return value(result)


def network_topology(s, *, page=1, page_size=20, feeder=None, q="", period="7d"):
    # A page contains at most 20 transformers and at most 12 meter previews each.
    _bounds(page, page_size)
    if page_size > 20:
        raise ValueError("topology page_size is limited to 20")
    transformers = list_transformers(
        s, page=page, page_size=page_size, feeder=feeder, q=q, period=period
    )
    groups = {}
    for transformer in transformers["items"]:
        key = transformer["feeder_id"]
        if key not in groups:
            groups[key] = {
                "id": key,
                "code": transformer["feeder_code"] or "Unassigned feeder",
                "transformers": [],
            }
        transformer["meters"] = list_meters(
            s, transformer_id=transformer["id"], page_size=12, period=period
        )
        groups[key]["transformers"].append(transformer)
    return {
        **transformers,
        "items": list(groups.values()),
        "pagination_unit": "transformers",
        "preview_note": "Meter previews show at most 12 meters; open a transformer for its paginated meter list.",
    }


def network_heatmap(s, *, period="24h", page=1, page_size=25, start=None, end=None):
    start, end = time_window(period, start, end)
    rows = list_transformers(s, page=page, page_size=min(page_size, 25), start=start, end=end)
    ids = [item["id"] for item in rows["items"]]
    bucket, seconds = _bucket(s, Balance.start, start, end)
    values = (
        s.execute(
            select(
                Balance.asset_id,
                bucket,
                func.sum(Balance.input_kwh),
                func.sum(Balance.accounting_difference_kwh),
            )
            .where(
                Balance.asset_id.in_(ids),
                Balance.start >= start,
                Balance.start < end,
                Balance.status == "COMPLETE",
            )
            .group_by(Balance.asset_id, bucket)
            .order_by(Balance.asset_id, bucket)
            .limit(10000)
        ).all()
        if ids
        else []
    )
    cells = [
        {
            "asset_id": item[0],
            "time": value(datetime.fromtimestamp(int(item[1]) * seconds, UTC).replace(tzinfo=None)),
            "imbalance_percent": float(item[3] / item[2] * 100) if item[2] else None,
        }
        for item in values
    ]
    return {
        **rows,
        "cells": cells,
        "start": value(start),
        "end": value(end),
        "bucket_minutes": seconds // 60,
    }


def global_search(s, q, *, limit=12):
    if not 1 <= limit <= 30:
        raise ValueError("search limit must be between 1 and 30")
    q = q.strip()[:128]
    result = {
        "feeders": [],
        "transformers": [],
        "meters": [],
        "investigations": [],
        "anomalies": [],
    }
    if not q:
        return result
    feeder_rows = s.execute(
        select(Feeder.id, Feeder.code, Feeder.name)
        .where(
            or_(Feeder.code.contains(q, autoescape=True), Feeder.name.contains(q, autoescape=True))
        )
        .order_by(Feeder.code, Feeder.id)
        .limit(limit)
    ).all()
    result["feeders"] = [
        {
            "id": row.id,
            "label": row.code,
            "code": row.code,
            "name": row.name,
            "url": f"/feeders/{row.id}",
        }
        for row in feeder_rows
    ]
    for kind, key in (("TRANSFORMER", "transformers"), ("METER", "meters")):
        rows = s.execute(
            select(Asset.id, Asset.code, Asset.name)
            .where(
                Asset.kind == kind,
                or_(
                    Asset.code.contains(q, autoescape=True), Asset.name.contains(q, autoescape=True)
                ),
            )
            .order_by(Asset.code, Asset.id)
            .limit(limit)
        ).all()
        result[key] = [
            {
                "id": row.id,
                "label": row.code,
                "code": row.code,
                "name": row.name,
                "url": f"/{key}/{row.id}",
            }
            for row in rows
        ]
    rows = s.execute(
        select(Case.id, Case.case_no)
        .where(Case.case_no.contains(q, autoescape=True))
        .order_by(Case.id.desc())
        .limit(limit)
    ).all()
    result["investigations"] = [
        {
            "id": row.id,
            "label": row.case_no,
            "case_no": row.case_no,
            "url": f"/investigations/{row.id}",
        }
        for row in rows
    ]
    if q.removeprefix("AN-").isdigit():
        anomaly = s.get(Anomaly, int(q.removeprefix("AN-")))
        if anomaly:
            asset = s.get(Asset, anomaly.asset_id)
            result["anomalies"] = [
                {
                    "id": anomaly.id,
                    "label": f"AN-{anomaly.id} · {anomaly.anomaly_type}",
                    "url": f"/{'meters' if asset.kind == 'METER' else 'transformers'}/{asset.id}",
                }
            ]
    return result


def alert_summary(s, *, period="7d"):
    start, end = time_window(period)
    counts = dict(
        s.execute(
            select(Anomaly.severity, func.count(func.distinct(Anomaly.asset_id)))
            .where(Anomaly.status == "OPEN", Anomaly.start >= start, Anomaly.start < end)
            .group_by(Anomaly.severity)
        ).all()
    )
    investigations = list_investigations(s, page_size=5, status="OPEN")["items"]
    return {
        "critical_anomalies": counts.get("CRITICAL", 0),
        "high_anomalies": counts.get("HIGH", 0),
        "open_investigations": s.scalar(
            select(func.count()).select_from(Case).where(Case.status.in_(OPEN_CASES))
        )
        or 0,
        "items": [
            {
                "label": f"{item['case_no']} · {item['asset_code']}",
                "severity": item["severity"],
                "url": f"/investigations/{item['id']}",
            }
            for item in investigations
        ],
    }


def system_data(s):
    reference = now()
    workers = []
    rows = {row.service: row for row in s.scalars(select(Heartbeat).limit(32))}
    for name in ("quality", "aggregation", "loss", "anomaly"):
        heartbeat = rows.get(name)
        age = (reference - heartbeat.time).total_seconds() if heartbeat else None
        workers.append(
            {
                "service": name,
                "status": "HEALTHY"
                if age is not None and age <= 120
                else "DEGRADED"
                if heartbeat
                else "UNKNOWN",
                "last_heartbeat": value(heartbeat.time) if heartbeat else None,
                "processed_total": heartbeat.processed if heartbeat else None,
                "age_seconds": max(0, age) if age is not None else None,
                "processing_rate": None,
            }
        )
    pending = s.scalar(select(func.count()).select_from(Outbox).where(Outbox.sent.is_(False))) or 0
    quarantined = (
        s.scalar(
            select(func.count())
            .select_from(Quarantine)
            .where(Quarantine.created_at >= reference - timedelta(days=1))
        )
        or 0
    )
    failed_jobs = (
        s.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.status == "FAILED", Job.created_at >= reference - timedelta(days=1))
        )
        or 0
    )
    return {
        "services": [
            {"service": "API", "status": "HEALTHY", "detail": "Request served"},
            {"service": "MySQL", "status": "HEALTHY", "detail": "Database query succeeded"},
            *[
                {"service": name, "status": "UNKNOWN", "detail": "Active probe not configured"}
                for name in ("Kafka", "RabbitMQ", "Grafana")
            ],
        ],
        "workers": workers,
        "outbox_pending": pending,
        "quarantined_24h": quarantined,
        "failed_jobs_24h": failed_jobs,
        "kafka_consumer_lag": None,
        "rabbitmq_queue_depth": None,
        "database_connections": None,
        "worker_processing_rate": None,
        "scheduler_checkpoint": value(rows["scheduler"].time) if "scheduler" in rows else None,
        "status": "DEGRADED"
        if quarantined or failed_jobs or any(w["status"] != "HEALTHY" for w in workers)
        else "HEALTHY",
        "telemetry_note": "Broker lag, queue depth, connection counts and processing rates require exporters; missing measurements are unknown.",
    }


def safe_settings():
    cfg = settings()
    return [
        {
            "section": "Algorithms",
            "name": "Baseline window",
            "value": cfg.baseline_days,
            "unit": "days",
            "description": "Comparable UTC quarter-hour slots with the same meter topology.",
        },
        {
            "section": "Algorithms",
            "name": "Minimum baseline samples",
            "value": cfg.baseline_min_samples,
            "unit": "samples",
            "description": "Fewer samples suppress transformer anomaly evaluation.",
        },
        {
            "section": "Algorithms",
            "name": "Persistence",
            "value": cfg.persistence_required,
            "unit": "of 6 intervals",
            "description": "All six intervals must be consecutive and eligible.",
        },
        {
            "section": "Thresholds",
            "name": "Minimum deviation",
            "value": cfg.deviation_pp,
            "unit": "percentage points",
            "description": "Also must exceed three scaled MADs.",
        },
        {
            "section": "Thresholds",
            "name": "Minimum impact",
            "value": cfg.min_impact_kwh,
            "unit": "kWh",
            "description": "Minimum accounting difference for a candidate.",
        },
        {
            "section": "Thresholds",
            "name": "Reporting completeness",
            "value": 100,
            "unit": "%",
            "description": "MVP accounting requires every expected meter. The configured partial-coverage target is not active.",
        },
        {
            "section": "Thresholds",
            "name": "Configured completeness target",
            "value": cfg.min_completeness,
            "unit": "% (inactive)",
            "description": "Reserved for the future partial-coverage policy; does not relax current strict accounting.",
        },
        {
            "section": "Thresholds",
            "name": "Consumption drop",
            "value": 30,
            "unit": "% of median",
            "description": "At least seven same-slot observations and a meaningful baseline are required.",
        },
        {
            "section": "Thresholds",
            "name": "Consumption spike",
            "value": 250,
            "unit": "% of median",
            "description": "Meter comparison uses the existing deterministic algorithm.",
        },
        {
            "section": "Workers",
            "name": "Allowed lateness",
            "value": cfg.allowed_lateness_minutes,
            "unit": "minutes",
            "description": "Late flags remain visible; late revisions can repair projections.",
        },
        {
            "section": "Workers",
            "name": "Maximum retries",
            "value": cfg.max_retries,
            "unit": "attempts",
            "description": "Permanent processing failures go to quarantine.",
        },
        {
            "section": "System",
            "name": "Bulk ingestion limit",
            "value": cfg.max_bulk_readings,
            "unit": "readings",
            "description": "Maximum observations in one accepted batch.",
        },
        {
            "section": "Data retention",
            "name": "Retention automation",
            "value": "Not configured",
            "unit": "",
            "description": "No automatic destructive data-retention job is installed.",
        },
        {
            "section": "Kafka",
            "name": "Messaging",
            "value": "Kafka + transactional outbox",
            "unit": "",
            "description": "Connection endpoints and credentials are intentionally excluded.",
        },
        {
            "section": "UI",
            "name": "Timezone",
            "value": "UTC",
            "unit": "",
            "description": "All accounting, chart and event timestamps are UTC.",
        },
    ]
