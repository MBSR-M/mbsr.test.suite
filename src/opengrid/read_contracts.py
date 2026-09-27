"""Canonical, exact-Decimal dashboard and feeder read models.

SQL selects bounded projection ranges. Contract construction is the serialization
boundary; presentation-only float conversion in ui_queries is never used here.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import ceil
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import and_, case, cast, func, or_, select, union
from sqlalchemy.orm import aliased

from opengrid.contracts import dashboard as c
from opengrid.contracts import feeder as f
from opengrid.db import (
    Aggregate,
    Anomaly,
    Asset,
    Assignment,
    Balance,
    Case,
    Feeder,
    Interval,
    Reading,
)
from opengrid.ui_queries import OPEN_CASES, VALID, _assignment, _latest

D = Decimal


def _aware(timestamp):
    return timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp.astimezone(UTC)


def _decimal(item):
    return None if item is None else D(str(item))


def _filters(filters=None):
    if filters is None:
        end = datetime.now(UTC)
        filters = {"from_time": end - timedelta(days=7), "to_time": end, "granularity": "1h"}
    return c.DashboardFilterContract.model_validate(filters)


def _times(filters):
    return filters.from_time.astimezone(UTC).replace(tzinfo=None), filters.to_time.astimezone(
        UTC
    ).replace(tzinfo=None)


def _health_expression():
    return case(
        (Anomaly.id.is_not(None), Anomaly.severity),
        (Balance.status == "COMPLETE", "HEALTHY"),
        else_="UNKNOWN",
    )


def _slots(start, end):
    return max(0, ceil(_aware(end).timestamp() / 900) - ceil(_aware(start).timestamp() / 900))


def _transformers(filters):
    start, end = _times(filters)
    query = select(Asset.id).where(Asset.kind == "TRANSFORMER", Asset.active.is_(True))
    if filters.feeder_ids:
        query = query.where(Asset.feeder_id.in_(filters.feeder_ids))
    if filters.transformer_ids:
        query = query.where(Asset.id.in_(filters.transformer_ids))
    if filters.severity:
        query = query.outerjoin(
            Anomaly,
            and_(
                Anomaly.asset_id == Asset.id,
                Anomaly.id == _latest(Anomaly, start, end, Anomaly.status == "OPEN"),
            ),
        )
        query = query.outerjoin(
            Balance, and_(Balance.asset_id == Asset.id, Balance.id == _latest(Balance, start, end))
        )
        severities = [
            "HEALTHY"
            if str(getattr(item, "value", item)) == "NORMAL"
            else str(getattr(item, "value", item))
            for item in filters.severity
        ]
        query = query.where(_health_expression().in_(severities))
    return query


def _meters(filters, *, overlap=False):
    start, end = _times(filters)
    query = (
        select(Assignment.meter_id)
        .join(Asset, Asset.id == Assignment.meter_id)
        .where(
            Asset.active.is_(True),
            Assignment.transformer_id.in_(_transformers(filters)),
            Assignment.valid_from < end,
            or_(
                Assignment.valid_to.is_(None),
                Assignment.valid_to > (start if overlap else end - timedelta(microseconds=1)),
            ),
        )
    )
    return query.distinct()


def _assets(filters):
    return union(_transformers(filters), _meters(filters, overlap=True))


def _energy_query(filters):
    start, end = _times(filters)
    valid_downstream = Aggregate.valid > 0
    paired = and_(
        Balance.input_kwh.is_not(None),
        Aggregate.expected > 0,
        Aggregate.valid == Aggregate.expected,
    )
    return (
        select(
            func.sum(Balance.input_kwh).label("input_energy"),
            func.sum(case((valid_downstream, Balance.downstream_kwh))).label("downstream_energy"),
            func.count(Balance.id).label("rows"),
            func.sum(case((paired, 1), else_=0)).label("paired_rows"),
            func.sum(Aggregate.expected).label("expected"),
            func.sum(Aggregate.received).label("received"),
            func.sum(Aggregate.valid).label("valid"),
        )
        .select_from(Balance)
        .outerjoin(
            Aggregate,
            and_(Aggregate.asset_id == Balance.asset_id, Aggregate.start == Balance.start),
        )
        .where(
            Balance.asset_id.in_(_transformers(filters)),
            Balance.start >= start,
            Balance.start < end,
        )
    )


def _energy_values(row, expected_rows):
    upstream, downstream = _decimal(row.input_energy), _decimal(row.downstream_energy)
    aligned = row.rows > 0 and row.paired_rows == row.rows
    difference = (
        upstream - downstream
        if aligned and upstream is not None and downstream is not None
        else None
    )
    completeness = D(row.valid) / D(row.expected) * 100 if row.expected else None
    availability = (
        "UNAVAILABLE"
        if upstream is None and downstream is None
        else "AVAILABLE"
        if aligned and row.rows >= expected_rows
        else "PARTIAL"
    )
    reason = (
        None
        if availability == "AVAILABLE"
        else "No valid energy projections exist in the selected range."
        if availability == "UNAVAILABLE"
        else "Energy covers only observed projections; incomplete population or absent interval slots prevent a complete period balance."
    )
    return {
        "input_energy_kwh": upstream,
        "downstream_energy_kwh": downstream,
        "accounting_difference_kwh": difference,
        "accounting_difference_percent": difference / upstream * 100
        if difference is not None and upstream is not None and upstream > 0
        else None,
        "input_energy_source": "DERIVED" if upstream is not None else "UNAVAILABLE",
        "downstream_energy_source": "DERIVED" if downstream is not None else "UNAVAILABLE",
        "accounting_difference_source": "DERIVED" if difference is not None else "UNAVAILABLE",
        "energy_availability": availability,
        "availability_reason": reason,
        "data_completeness_percent": completeness,
        "confidence_score": None,
    }


def _health(s, filters):
    start, end = _times(filters)
    status = _health_expression()
    rows = s.execute(
        select(status, func.count())
        .select_from(Asset)
        .outerjoin(
            Anomaly,
            and_(
                Anomaly.asset_id == Asset.id,
                Anomaly.id == _latest(Anomaly, start, end, Anomaly.status == "OPEN"),
            ),
        )
        .outerjoin(
            Balance, and_(Balance.asset_id == Asset.id, Balance.id == _latest(Balance, start, end))
        )
        .where(Asset.id.in_(_transformers(filters)))
        .group_by(status)
    ).all()
    mapping = dict(rows)
    return {
        name.lower(): mapping.get(name, 0)
        for name in ("HEALTHY", "WATCH", "HIGH", "CRITICAL", "UNKNOWN")
    }


def _summary_data(s, filters):
    start, end = _times(filters)
    transformer_count = (
        s.scalar(select(func.count()).select_from(_transformers(filters).subquery())) or 0
    )
    meter_count = s.scalar(select(func.count()).select_from(_meters(filters).subquery())) or 0
    feeder_query = select(func.count(func.distinct(Asset.feeder_id))).where(
        Asset.id.in_(_transformers(filters))
    )
    energy = s.execute(_energy_query(filters)).one()
    expected_rows = transformer_count * _slots(start, end)
    anomaly_counts = dict(
        s.execute(
            select(Anomaly.severity, func.count())
            .where(
                Anomaly.asset_id.in_(_assets(filters)),
                Anomaly.start >= start,
                Anomaly.start < end,
                Anomaly.status == "OPEN",
            )
            .group_by(Anomaly.severity)
        ).all()
    )
    cases = (
        s.scalar(
            select(func.count())
            .select_from(Case)
            .where(Case.asset_id.in_(_assets(filters)), Case.status.in_(OPEN_CASES))
        )
        or 0
    )
    health = _health(s, filters)
    data = {
        "period_start": filters.from_time,
        "period_end": filters.to_time,
        "transformer_count": transformer_count,
        "meter_count": meter_count,
        "feeder_count": s.scalar(feeder_query) or 0,
        **_energy_values(energy, expected_rows),
        "open_investigation_count": cases,
        "critical_anomaly_count": anomaly_counts.get("CRITICAL", 0),
        "high_anomaly_count": anomaly_counts.get("HIGH", 0),
        "watch_anomaly_count": anomaly_counts.get("WATCH", 0),
        **{f"{key}_transformer_count": count for key, count in health.items()},
    }
    return data, health, energy


def canonical_dashboard_summary(s, filters=None):
    filters = _filters(filters)
    return c.DashboardSummaryContract(**_summary_data(s, filters)[0])


def canonical_dashboard_trend(s, filters=None):
    filters = _filters(filters)
    start, end = _times(filters)
    minutes = {"15m": 15, "1h": 60, "1d": 1440}[filters.granularity]
    seconds = minutes * 60
    # MySQL epoch conversion is safe because the deployment stores/session-renders UTC.
    bucket = func.floor(func.unix_timestamp(Balance.start) / seconds)
    query = (
        _energy_query(filters)
        .add_columns(bucket.label("bucket"))
        .group_by(bucket)
        .order_by(bucket)
        .limit(3001)
    )
    rows = {int(row.bucket): row for row in s.execute(query)}
    transformer_count = (
        s.scalar(select(func.count()).select_from(_transformers(filters).subquery())) or 0
    )
    anomaly_bucket = func.floor(func.unix_timestamp(Anomaly.start) / seconds)
    anomalies = dict(
        s.execute(
            select(anomaly_bucket, func.count())
            .where(
                Anomaly.asset_id.in_(_assets(filters)),
                Anomaly.start >= start,
                Anomaly.start < end,
                Anomaly.status == "OPEN",
            )
            .group_by(anomaly_bucket)
            .limit(3001)
        ).all()
    )
    points = []
    cursor = int(_aware(start).timestamp()) // seconds
    last = int((_aware(end) - timedelta(microseconds=1)).timestamp()) // seconds
    if last - cursor + 1 > 3000:
        raise ValueError("trend is limited to 3000 points; choose a coarser granularity")
    while cursor <= last:
        row = rows.get(cursor)
        bucket_start = max(datetime.fromtimestamp(cursor * seconds, UTC), filters.from_time)
        bucket_end = min(datetime.fromtimestamp((cursor + 1) * seconds, UTC), filters.to_time)
        if row:
            metrics = _energy_values(row, transformer_count * _slots(bucket_start, bucket_end))
        else:
            metrics = {
                "input_energy_kwh": None,
                "downstream_energy_kwh": None,
                "accounting_difference_kwh": None,
                "accounting_difference_percent": None,
                "input_energy_source": "UNAVAILABLE",
                "downstream_energy_source": "UNAVAILABLE",
                "accounting_difference_source": "UNAVAILABLE",
                "energy_availability": "UNAVAILABLE",
                "availability_reason": "No calculated interval projection exists for this time bucket.",
                "data_completeness_percent": None,
                "confidence_score": None,
            }
        # Confidence is a finding-level property, not a reconstructed chart metric.
        metrics.pop("confidence_score")
        points.append(
            c.DashboardTrendPointContract(
                timestamp=bucket_start,
                anomaly_count=anomalies.get(cursor, 0),
                **metrics,
            )
        )
        cursor += 1
    return c.DashboardTrendContract(
        period_start=filters.from_time,
        period_end=filters.to_time,
        granularity=filters.granularity,
        points=points,
    )


def _top_transformers(s, filters):
    start, end = _times(filters)
    case_count = (
        select(func.count())
        .select_from(Case)
        .where(Case.asset_id == Asset.id, Case.status.in_(OPEN_CASES))
        .correlate(Asset)
        .scalar_subquery()
    )
    rows = s.execute(
        select(Asset, Feeder, Anomaly, case_count)
        .outerjoin(Feeder, Feeder.id == Asset.feeder_id)
        .join(
            Anomaly,
            and_(
                Anomaly.asset_id == Asset.id,
                Anomaly.id == _latest(Anomaly, start, end, Anomaly.status == "OPEN"),
            ),
        )
        .where(Asset.id.in_(_transformers(filters)))
        .order_by(Anomaly.score.desc(), Asset.id)
        .limit(10)
    ).all()
    items = []
    for asset, feeder, finding, count in rows:
        evidence = finding.evidence
        expected, valid = evidence.get("expected_meter_count"), evidence.get("valid_meter_count")
        items.append(
            c.DashboardTransformerItemContract(
                transformer_id=asset.id,
                transformer_code=asset.code,
                feeder_id=feeder.id if feeder else None,
                feeder_code=feeder.code if feeder else None,
                current_imbalance_percent=_decimal(evidence.get("accounting_difference_percent")),
                baseline_imbalance_percent=_decimal((evidence.get("baseline") or {}).get("median")),
                deviation_percentage_points=_decimal(evidence.get("deviation_pp")),
                accounting_difference_kwh=_decimal(evidence.get("accounting_difference_kwh")),
                persistence_intervals=evidence.get("persistence_slots"),
                data_completeness_percent=D(valid) / D(expected) * 100
                if expected and valid is not None
                else None,
                confidence_score=_decimal(evidence.get("confidence_score")),
                anomaly_score=finding.score,
                severity=finding.severity,
                open_investigation_count=count,
            )
        )
    return items


def _top_meters(s, filters):
    start, end = _times(filters)
    transformer = aliased(Asset)
    last_reading = (
        select(Reading.time)
        .where(Reading.asset_id == Asset.id, Reading.time >= start, Reading.time < end)
        .order_by(Reading.time.desc())
        .limit(1)
        .correlate(Asset)
        .scalar_subquery()
    )
    rows = s.execute(
        select(Asset, transformer, Anomaly, Interval, last_reading)
        .join(
            Assignment,
            and_(
                Assignment.meter_id == Asset.id,
                Assignment.id == _assignment(end - timedelta(microseconds=1)),
            ),
        )
        .join(transformer, transformer.id == Assignment.transformer_id)
        .join(
            Anomaly,
            and_(
                Anomaly.asset_id == Asset.id,
                Anomaly.id == _latest(Anomaly, start, end, Anomaly.status == "OPEN"),
            ),
        )
        .outerjoin(Interval, and_(Interval.asset_id == Asset.id, Interval.start == Anomaly.start))
        .where(Asset.id.in_(_meters(filters)))
        .order_by(Anomaly.score.desc(), Asset.id)
        .limit(10)
    ).all()
    items = []
    for asset, transformer, finding, interval, last in rows:
        current = interval.import_kwh if interval and interval.status in VALID else None
        baseline = _decimal(finding.evidence.get("mean_28_days"))
        items.append(
            c.DashboardMeterItemContract(
                meter_id=asset.id,
                meter_no=asset.code,
                transformer_id=transformer.id,
                transformer_code=transformer.code,
                anomaly_type=finding.anomaly_type,
                current_consumption_kwh=current,
                baseline_consumption_kwh=baseline,
                deviation_percent=(current - baseline) / baseline * 100
                if current is not None and baseline
                else None,
                data_completeness_percent=None,
                anomaly_score=finding.score,
                severity=finding.severity,
                last_reading_time=_aware(last) if last else None,
            )
        )
    return items


def _top_cases(s, filters):
    rows = s.execute(
        select(Case, Asset, Anomaly)
        .join(Asset, Asset.id == Case.asset_id)
        .join(Anomaly, Anomaly.id == Case.anomaly_id)
        .where(Case.asset_id.in_(_assets(filters)), Case.status.in_(OPEN_CASES))
        .order_by(Case.priority.desc(), Case.id)
        .limit(10)
    ).all()
    return [
        c.DashboardInvestigationItemContract(
            investigation_id=investigation.id,
            case_no=investigation.case_no,
            entity_type=asset.kind,
            entity_id=asset.id,
            entity_display_name=asset.code,
            priority_score=investigation.priority,
            severity=finding.severity,
            estimated_unaccounted_kwh=_decimal(
                investigation.evidence.get("accounting_difference_kwh")
            ),
            status=investigation.status,
            assigned_to=investigation.assigned_to,
            opened_at=_aware(investigation.opened_at),
        )
        for investigation, asset, finding in rows
    ]


def _quality(s, filters, energy):
    start, end = _times(filters)
    from sqlalchemy import String

    flags = cast(Interval.evidence["flags"], String)
    query = (
        select(
            func.sum(case((flags.contains('"LATE"'), 1), else_=0)),
            func.sum(case((Interval.status.not_in((*VALID, "MISSING")), 1), else_=0)),
            func.count(
                func.distinct(
                    case((and_(Asset.kind == "METER", Interval.status.not_in(VALID)), Asset.id))
                )
            ),
            func.count(
                func.distinct(
                    case(
                        (and_(Asset.kind == "TRANSFORMER", Interval.status.not_in(VALID)), Asset.id)
                    )
                )
            ),
        )
        .select_from(Interval)
        .join(Asset, Asset.id == Interval.asset_id)
        .where(
            Interval.asset_id.in_(_assets(filters)), Interval.start >= start, Interval.start < end
        )
    )
    late, invalid, affected_meters, affected_transformers = s.execute(query).one()
    return c.DashboardDataQualityContract(
        period_start=filters.from_time,
        period_end=filters.to_time,
        completeness_percent=D(energy.valid) / D(energy.expected) * 100
        if energy.expected
        else None,
        expected_intervals=int(energy.expected) if energy.expected is not None else None,
        received_intervals=int(energy.received) if energy.received is not None else None,
        missing_intervals=max(0, int(energy.expected - energy.received))
        if energy.expected is not None
        else None,
        duplicate_intervals=None,
        late_intervals=int(late or 0),
        invalid_intervals=int(invalid or 0),
        affected_meter_count=affected_meters,
        affected_transformer_count=affected_transformers,
        availability_reason="Coverage counts describe calculated transformer aggregates; absent unprocessed slots are unknown. Duplicate receipts are idempotent and no duplicate counter is stored.",
    )


def _activity(s, filters):
    # The event service is paginated; build activity directly from the matching
    # scoped UNION so filtered dashboards cannot leak a different feeder's events.
    from sqlalchemy import union_all

    from opengrid.ui_queries import _event_queries

    start, end = _times(filters)
    events = union_all(*_event_queries(start, end)).subquery()
    rows = (
        s.execute(
            select(events)
            .where(events.c.asset_id.in_(_assets(filters)))
            .order_by(events.c.time.desc(), events.c.source, events.c.id)
            .limit(12)
        )
        .mappings()
        .all()
    )
    items = []
    for row in rows:
        source = row["source"]
        kind = (
            "ANOMALY"
            if source == "anomaly"
            else "INVESTIGATION"
            if source == "case-history"
            else "COMMUNICATION"
            if row["event_type"].startswith("COMMUNICATION")
            else row["event_type"]
            if row["event_type"] in {"READING", "QUALITY"}
            else "SYSTEM"
        )
        severity = "WARNING" if row["severity"] == "WATCH" else row["severity"]
        items.append(
            c.DashboardActivityContract(
                activity_id=str(uuid5(NAMESPACE_URL, f"opengrid/{source}/{row['id']}")),
                timestamp=_aware(row["time"]),
                activity_type=kind,
                entity_type=row["asset_kind"],
                entity_id=row["asset_id"],
                title=row["event_type"].replace("_", " ").capitalize(),
                description=f"{row['asset_code']} · {source}",
                severity=severity,
            )
        )
    return items


def canonical_dashboard(s, filters=None):
    filters = _filters(filters)
    summary, health, energy = _summary_data(s, filters)
    return c.DashboardContract(
        generated_at=datetime.now(UTC),
        filters=filters,
        summary=c.DashboardSummaryContract(**summary),
        trend=canonical_dashboard_trend(s, filters),
        health=c.DashboardHealthDistributionContract(**health),
        top_transformers=_top_transformers(s, filters),
        top_meters=_top_meters(s, filters),
        top_investigations=_top_cases(s, filters),
        data_quality=_quality(s, filters, energy),
        recent_activity=_activity(s, filters),
    )


def canonical_feeder(s, feeder_id):
    feeder = s.get(Feeder, feeder_id)
    if feeder is None:
        return None
    filters = _filters().model_copy(update={"feeder_ids": [feeder_id]})
    transformers = (
        s.scalar(select(func.count()).select_from(_transformers(filters).subquery())) or 0
    )
    meters = s.scalar(select(func.count()).select_from(_meters(filters).subquery())) or 0
    return f.FeederContract(
        feeder_id=feeder.id,
        feeder_code=feeder.code,
        name=feeder.name or None,
        substation=None,
        active=feeder.active,
        transformer_count=transformers,
        meter_count=meters,
        created_at=None,
        updated_at=None,
        metadata_unavailable_reason="Legacy feeder metadata does not record substation or creation/update timestamps.",
    )


def feeder_summary(s, feeder_id, filters=None):
    feeder = s.get(Feeder, feeder_id)
    if feeder is None:
        return None
    filters = _filters(filters).model_copy(update={"feeder_ids": [feeder_id]})
    summary, health, energy = _summary_data(s, filters)
    status = next(
        (
            name.upper()
            for name in ("critical", "high", "watch", "unknown", "healthy")
            if health[name]
        ),
        "UNKNOWN",
    )
    return f.FeederSummaryContract(
        period_start=filters.from_time,
        period_end=filters.to_time,
        feeder_id=feeder.id,
        feeder_code=feeder.code,
        name=feeder.name or None,
        substation=None,
        transformer_count=summary["transformer_count"],
        meter_count=summary["meter_count"],
        **{key: summary[key] for key in _energy_values(energy, 0)},
        anomaly_count=summary["critical_anomaly_count"]
        + summary["high_anomaly_count"]
        + summary["watch_anomaly_count"],
        high_anomaly_count=summary["high_anomaly_count"],
        critical_anomaly_count=summary["critical_anomaly_count"],
        open_investigation_count=summary["open_investigation_count"],
        status=status,
    )


def feeder_trends(s, feeder_id, filters=None):
    if s.get(Feeder, feeder_id) is None:
        return None
    filters = _filters(filters).model_copy(update={"feeder_ids": [feeder_id]})
    trend = canonical_dashboard_trend(s, filters)
    return f.FeederTrendContract(
        period_start=filters.from_time,
        period_end=filters.to_time,
        feeder_id=feeder_id,
        interval_minutes={"15m": 15, "1h": 60, "1d": 1440}[filters.granularity],
        points=[f.FeederTrendPointContract(**point.model_dump()) for point in trend.points],
    )


def feeder_topology(s, feeder_id, *, page=1, page_size=20):
    from opengrid.ui_queries import _bounds

    _bounds(page, page_size)
    if page_size > 20:
        raise ValueError("topology page_size is limited to 20")
    feeder = canonical_feeder(s, feeder_id)
    if feeder is None:
        return None
    filters = _filters().model_copy(update={"feeder_ids": [feeder_id]})
    start, end = _times(filters)
    rows = s.execute(
        select(Asset, _health_expression())
        .outerjoin(
            Balance, and_(Balance.asset_id == Asset.id, Balance.id == _latest(Balance, start, end))
        )
        .outerjoin(
            Anomaly,
            and_(
                Anomaly.asset_id == Asset.id,
                Anomaly.id == _latest(Anomaly, start, end, Anomaly.status == "OPEN"),
            ),
        )
        .where(Asset.id.in_(_transformers(filters)))
        .order_by(Asset.code, Asset.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    transformers = []
    for transformer, status in rows:
        scope = filters.model_copy(update={"transformer_ids": [transformer.id]})
        meter_count = s.scalar(select(func.count()).select_from(_meters(scope).subquery())) or 0
        meters = s.scalars(
            select(Asset)
            .where(Asset.id.in_(_meters(scope)))
            .order_by(Asset.code, Asset.id)
            .limit(20)
        ).all()
        transformers.append(
            f.TransformerTopologyContract(
                transformer_id=transformer.id,
                transformer_code=transformer.code,
                name=transformer.name or None,
                meter_count=meter_count,
                status=status,
                meters_truncated=meter_count > len(meters),
                meters=[
                    f.MeterTopologyContract(
                        meter_id=meter.id, meter_no=meter.code, consumer_type=None, status="UNKNOWN"
                    )
                    for meter in meters
                ],
            )
        )
    return f.FeederTopologyContract(
        feeder=feeder,
        transformers=transformers,
        page=page,
        page_size=page_size,
        total=feeder.transformer_count,
        pages=ceil(feeder.transformer_count / page_size),
    )
