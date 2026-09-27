"""UI read-model contracts, exercised against the real MySQL dialect."""

from datetime import UTC, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from opengrid import ui_queries as ui
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
    Interval,
    MeterEvent,
    Reading,
    now,
    session_factory,
)


@pytest.fixture
def dataset():
    """Rollback the fixture so UI checks never contaminate the demonstration."""
    factory = session_factory()
    with factory() as session:
        transaction = session.begin()
        try:
            stamp = now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
            suffix = uuid4().hex[:12]
            feeder = Feeder(code=f"UI-F-{suffix}")
            session.add(feeder)
            session.flush()
            transformer = Asset(code=f"UI-T-{suffix}", kind="TRANSFORMER", feeder_id=feeder.id)
            meter = Asset(code=f"UI-M-{suffix}", kind="METER")
            session.add_all([transformer, meter])
            session.flush()
            session.add_all(
                [
                    Assignment(
                        meter_id=meter.id,
                        transformer_id=transformer.id,
                        valid_from=stamp - timedelta(days=40),
                    ),
                    Configuration(
                        asset_id=meter.id,
                        valid_from=stamp - timedelta(days=40),
                        import_only=True,
                        multiplier=1,
                    ),
                    Interval(
                        asset_id=meter.id,
                        start=stamp,
                        import_kwh=Decimal("0.2"),
                        export_kwh=0,
                        status="VALID",
                        evidence={"flags": ["LATE"]},
                    ),
                    Reading(
                        event_id=str(uuid4()),
                        asset_id=meter.id,
                        time=stamp + timedelta(minutes=15),
                        source="UI_TEST",
                        revision=1,
                        kind="INTERVAL_ENERGY",
                        epoch="ui-test",
                        primary=True,
                        import_value=Decimal("0.2"),
                        export_value=0,
                        received_at=stamp + timedelta(minutes=15),
                        payload={},
                        digest="ui-test",
                    ),
                    Balance(
                        asset_id=transformer.id,
                        start=stamp,
                        input_kwh=100,
                        downstream_kwh=Decimal("81.3"),
                        accounting_difference_kwh=Decimal("18.7"),
                        accounting_difference_percent=Decimal("18.7"),
                        completeness=100,
                        status="COMPLETE",
                        fingerprint="ui-complete",
                        evidence={},
                    ),
                    Aggregate(
                        asset_id=transformer.id,
                        start=stamp,
                        expected=1,
                        received=1,
                        valid=1,
                        downstream_kwh=Decimal("81.3"),
                        fingerprint="ui-aggregate",
                        evidence={},
                    ),
                ]
            )
            for day in range(1, 9):
                session.add(
                    Interval(
                        asset_id=meter.id,
                        start=stamp - timedelta(days=day),
                        import_kwh=1,
                        export_kwh=0,
                        status="VALID",
                        evidence={},
                    )
                )
            evidence = {
                "algorithm": "anomaly-v1",
                "accounting_difference_percent": "18.7",
                "accounting_difference_kwh": "18.7",
                "baseline": {"median": "7.2"},
                "deviation_pp": "11.5",
                "persistence_slots": 6,
                "window_slots": 6,
                "expected_meter_count": 1,
                "valid_meter_count": 1,
                "confidence_score": "100",
                "historical_samples": 28,
                "score": "75.125",
                "abnormal_meters": [{"meter_id": meter.id, "type": "CONSUMPTION_DROP"}],
                "relevant_event_ids": [],
            }
            anomaly = Anomaly(
                asset_id=transformer.id,
                start=stamp,
                anomaly_type="ENERGY_IMBALANCE",
                severity="HIGH",
                score=75,
                status="OPEN",
                evidence=evidence,
            )
            session.add(anomaly)
            session.flush()
            investigation = Case(
                case_no=f"UI-CASE-{suffix}",
                asset_id=transformer.id,
                anomaly_id=anomaly.id,
                status="NEW",
                priority=75,
                evidence=evidence,
                opened_at=stamp,
            )
            session.add_all(
                [
                    investigation,
                    Evaluation(
                        asset_id=transformer.id,
                        start=stamp,
                        eligible=True,
                        abnormal=True,
                        evidence=evidence,
                    ),
                    Anomaly(
                        asset_id=meter.id,
                        start=stamp,
                        anomaly_type="CONSUMPTION_DROP",
                        severity="WATCH",
                        score=30,
                        status="OPEN",
                        evidence={
                            "algorithm": "meter-v1",
                            "type": "CONSUMPTION_DROP",
                            "mean_7_days": "1",
                            "mean_28_days": "1",
                            "history_samples": 8,
                        },
                    ),
                    MeterEvent(
                        id=str(uuid4()),
                        asset_id=meter.id,
                        time=stamp,
                        event_type="POWER_FAILURE",
                        evidence={"source": "test"},
                    ),
                ]
            )
            session.flush()
            session.add(
                Audit(
                    case_id=investigation.id,
                    actor="ui-test",
                    time=stamp,
                    body={"action": "CREATED"},
                )
            )
            session.flush()
            yield session, transformer, meter, investigation, stamp, suffix
        finally:
            transaction.rollback()
            factory.kw["bind"].dispose()


@pytest.mark.integration
def test_transformer_filter_sort_and_paginated_contract(dataset):
    session, transformer, meter, investigation, stamp, suffix = dataset
    result = ui.list_transformers(session, q=suffix, sort="score", order="desc", page_size=1)
    assert result["total"] == result["pages"] == 1
    row = result["items"][0]
    assert row["id"] == transformer.id
    assert row["meter_count"] == 1
    assert row["imbalance_percent"] == pytest.approx(18.7)
    assert row["baseline_percent"] == pytest.approx(7.2)
    assert row["investigation_id"] == investigation.id
    assert ui.list_transformers(session, q=suffix, severity="CRITICAL")["items"] == []
    assert ui.list_transformers(session, q=suffix, page=2, page_size=1)["items"] == []
    assert ui.list_transformers(session, q=suffix, min_imbalance=20)["total"] == 0


@pytest.mark.integration
def test_incomplete_latest_balance_does_not_overwrite_flagged_evidence(dataset):
    session, transformer, meter, investigation, stamp, suffix = dataset
    session.add(
        Balance(
            asset_id=transformer.id,
            start=stamp + timedelta(minutes=15),
            input_kwh=None,
            downstream_kwh=0,
            accounting_difference_kwh=None,
            accounting_difference_percent=None,
            completeness=0,
            status="MISSING_TRANSFORMER",
            fingerprint="ui-missing",
            evidence={},
        )
    )
    session.flush()
    detail = ui.transformer_detail(session, transformer.id)
    assert detail["imbalance_percent"] is None
    assert detail["status"] == "MISSING_TRANSFORMER"
    assert detail["flagged_imbalance_percent"] == pytest.approx(18.7)
    assert detail["flagged_baseline_percent"] == pytest.approx(7.2)
    assert detail["affected_meters"]["items"][0]["id"] == meter.id
    assert "6 of 6" in " ".join(detail["explanation"])


@pytest.mark.integration
def test_meter_baseline_quality_and_current_mapping(dataset):
    session, transformer, meter, investigation, stamp, suffix = dataset
    row = ui.list_meters(session, transformer_id=transformer.id)["items"][0]
    assert row["transformer_id"] == transformer.id
    assert row["baseline_28d"] == pytest.approx(1)
    assert row["deviation_percent"] == pytest.approx(-80)
    assert row["last_reading"].endswith("Z")
    detail = ui.meter_detail(session, meter.id)
    assert detail["baseline_7d"] == pytest.approx(1)
    assert detail["quality"]["late"] == 1
    assert detail["quality"]["duplicates"] is None
    assert detail["consumer_type"] is None


@pytest.mark.integration
def test_aggregate_charts_do_not_convert_missing_measurements_to_zero(dataset):
    session, transformer, meter, investigation, stamp, suffix = dataset
    session.add(
        Balance(
            asset_id=transformer.id,
            start=stamp + timedelta(minutes=15),
            input_kwh=None,
            downstream_kwh=0,
            accounting_difference_kwh=None,
            accounting_difference_percent=None,
            completeness=0,
            status="MISSING_TRANSFORMER",
            fingerprint="ui-missing",
            evidence={},
        )
    )
    session.flush()
    trend = ui.dashboard_trends(
        session,
        transformer_id=transformer.id,
        period="24h",
        start=stamp,
        end=stamp + timedelta(hours=1),
    )
    assert len(trend["points"]) == 2
    assert trend["points"][0]["imbalance_percent"] == pytest.approx(18.7)
    assert trend["points"][1]["input_kwh"] is None
    assert trend["points"][1]["imbalance_percent"] is None
    assert trend["points"][0]["baseline_percent"] == pytest.approx(7.2)


@pytest.mark.integration
def test_search_is_bounded_and_escapes_wildcards(dataset):
    session, transformer, meter, investigation, stamp, suffix = dataset
    results = ui.global_search(session, suffix, limit=1)
    assert results["transformers"][0]["id"] == transformer.id
    assert results["meters"][0]["id"] == meter.id
    assert results["investigations"][0]["id"] == investigation.id
    assert all(not items for items in ui.global_search(session, "%_' OR 1=1").values())
    assert ui.global_search(session, f"AN-{investigation.anomaly_id}")["anomalies"]


@pytest.mark.integration
def test_investigation_details_use_original_snapshot(dataset):
    session, transformer, meter, investigation, stamp, suffix = dataset
    queue = ui.list_investigations(session, q=suffix, status="NEW")
    assert queue["total"] == 1
    detail = ui.investigation_detail(session, investigation.id)
    assert detail["imbalance_percent"] == pytest.approx(18.7)
    assert detail["timeline"]["items"][0]["details"]["action"] == "CREATED"
    assert detail["affected_meters"]["items"][0]["id"] == meter.id
    assert ui.investigation_detail(session, -1) is None


@pytest.mark.integration
def test_event_union_filters_include_effective_mapped_meter_events(dataset):
    session, transformer, meter, investigation, stamp, suffix = dataset
    events = ui.list_events(session, transformer_id=transformer.id, event_type="POWER_FAILURE")
    assert events["total"] == 1
    assert events["items"][0]["asset_id"] == meter.id
    assert events["items"][0]["url"] == f"/meters/{meter.id}"
    timeline = ui.entity_timeline(session, transformer.id, include_meters=True)
    assert {"READING", "INVESTIGATION", "ENERGY_IMBALANCE"}.issubset(
        {item["event_type"] for item in timeline["items"]}
    )


@pytest.mark.integration
def test_quality_filters_and_missing_counter_meaning(dataset):
    session, transformer, meter, investigation, stamp, suffix = dataset
    rows = ui.list_quality(session, transformer_id=transformer.id, status="LATE")
    assert rows["total"] == 1
    summary = ui.quality_summary(
        session, asset_id=meter.id, start=stamp, end=stamp + timedelta(hours=1)
    )
    assert summary["expected_intervals"] == 4
    assert summary["valid"] == 1
    assert summary["missing"] == 3
    assert summary["completeness_percent"] == pytest.approx(25)
    assert summary["duplicates"] is None


@pytest.mark.integration
def test_dashboard_topology_heatmap_and_system_execute_on_mysql(dataset):
    session, transformer, meter, investigation, stamp, suffix = dataset
    summary = ui.dashboard_summary(session)
    assert summary["transformers"] >= 1
    assert summary["meters"] >= 1
    assert "UNKNOWN" in summary["network_health"]
    topology = ui.network_topology(session, q=suffix)
    child = topology["items"][0]["transformers"][0]
    assert child["meters"]["items"][0]["id"] == meter.id
    heatmap = ui.network_heatmap(session, page_size=25)
    assert len(heatmap["items"]) <= 25
    system = ui.system_data(session)
    assert system["kafka_consumer_lag"] is None
    assert system["database_connections"] is None
    assert all("password" not in str(row).lower() for row in system["services"])
    assert ui.alert_summary(session)["open_investigations"] >= 1


def test_query_bounds_and_recorded_evidence_explanations():
    with pytest.raises(ValueError):
        ui.time_window("forever")
    with pytest.raises(ValueError):
        ui.time_window(start=now() - timedelta(days=91))
    with pytest.raises(ValueError):
        ui._bounds(1, 101)
    assert "No detailed" in ui.evidence_explanation({})[0]
    assert ui.score_components({}) == []
    assert "98.9" not in " ".join(ui.evidence_explanation({}))
    assert ui.number("NaN") is None
    assert ui.number("Infinity") is None


def test_settings_are_allowlisted_and_strict_completeness_is_explicit():
    rows = ui.safe_settings()
    assert next(row for row in rows if row["name"] == "Reporting completeness")["value"] == 100
    assert not any(
        secret in str(rows).lower() for secret in ("database_url", "api_key", "rabbitmq_password")
    )


def canonical_filters(transformer, stamp, minutes=60):
    return {
        "from_time": stamp.replace(tzinfo=UTC),
        "to_time": (stamp + timedelta(minutes=minutes)).replace(tzinfo=UTC),
        "transformer_ids": [transformer.id],
        "granularity": "15m",
    }


@pytest.mark.integration
def test_canonical_dashboard_preserves_decimals_partial_and_explicit_gaps(dataset):
    from opengrid.read_contracts import canonical_dashboard

    session, transformer, meter, investigation, stamp, suffix = dataset
    result = canonical_dashboard(session, canonical_filters(transformer, stamp))
    wire = result.model_dump(mode="json")
    assert isinstance(wire["summary"]["input_energy_kwh"], str)
    assert Decimal(wire["summary"]["input_energy_kwh"]) == Decimal("100")
    assert Decimal(wire["summary"]["accounting_difference_kwh"]) == Decimal("18.7")
    assert wire["summary"]["energy_availability"] == "PARTIAL"
    assert wire["summary"]["input_energy_source"] == "DERIVED"
    assert len(wire["trend"]["points"]) == 4
    assert wire["trend"]["points"][1]["input_energy_kwh"] is None
    assert wire["trend"]["points"][1]["energy_availability"] == "UNAVAILABLE"
    assert wire["top_transformers"][0]["transformer_id"] == transformer.id
    assert wire["top_meters"][0]["meter_id"] == meter.id
    assert wire["top_investigations"][0]["investigation_id"] == investigation.id
    assert wire["data_quality"]["duplicate_intervals"] is None


@pytest.mark.integration
def test_canonical_valid_zero_energy_is_not_missing_or_zero_percent(dataset):
    from sqlalchemy import select

    from opengrid.read_contracts import canonical_dashboard_summary

    session, transformer, meter, investigation, stamp, suffix = dataset
    balance = session.scalar(select(Balance).where(Balance.asset_id == transformer.id))
    balance.input_kwh = balance.downstream_kwh = balance.accounting_difference_kwh = Decimal("0")
    balance.accounting_difference_percent = None
    balance.status = "NON_POSITIVE_NET_INPUT"
    session.flush()
    result = canonical_dashboard_summary(session, canonical_filters(transformer, stamp, 15))
    assert result.input_energy_kwh == Decimal("0")
    assert result.downstream_energy_kwh == Decimal("0")
    assert result.accounting_difference_kwh == Decimal("0")
    assert result.accounting_difference_percent is None
    assert result.energy_availability == "AVAILABLE"


@pytest.mark.integration
def test_canonical_missing_downstream_never_fabricates_period_difference(dataset):
    from opengrid.read_contracts import canonical_dashboard_summary

    session, transformer, meter, investigation, stamp, suffix = dataset
    session.add_all(
        [
            Balance(
                asset_id=transformer.id,
                start=stamp + timedelta(minutes=15),
                input_kwh=100,
                downstream_kwh=0,
                accounting_difference_kwh=100,
                accounting_difference_percent=None,
                completeness=0,
                status="INCOMPLETE",
                fingerprint="canonical-missing",
                evidence={},
            ),
            Aggregate(
                asset_id=transformer.id,
                start=stamp + timedelta(minutes=15),
                expected=1,
                received=0,
                valid=0,
                downstream_kwh=0,
                fingerprint="canonical-missing",
                evidence={},
            ),
        ]
    )
    session.flush()
    result = canonical_dashboard_summary(session, canonical_filters(transformer, stamp, 30))
    assert result.input_energy_kwh == Decimal("200")
    assert result.downstream_energy_kwh == Decimal("81.3")
    assert result.accounting_difference_kwh is None
    assert result.accounting_difference_percent is None
    assert result.data_completeness_percent == Decimal("50")
    assert result.energy_availability == "PARTIAL"


@pytest.mark.integration
def test_canonical_empty_network_energy_stays_unavailable(dataset):
    from opengrid.read_contracts import canonical_dashboard

    session, transformer, meter, investigation, stamp, suffix = dataset
    empty = Asset(code=f"EMPTY-{suffix}", kind="TRANSFORMER")
    session.add(empty)
    session.flush()
    result = canonical_dashboard(session, canonical_filters(empty, stamp))
    assert result.summary.input_energy_kwh is None
    assert result.summary.downstream_energy_kwh is None
    assert result.summary.data_completeness_percent is None
    assert result.summary.energy_availability == "UNAVAILABLE"
    assert result.summary.unknown_transformer_count == 1
    assert result.data_quality.expected_intervals is None
    assert result.top_transformers == []


@pytest.mark.integration
def test_canonical_feeder_contracts_are_scoped_and_paginated(dataset):
    from opengrid.read_contracts import (
        canonical_feeder,
        feeder_summary,
        feeder_topology,
        feeder_trends,
    )

    session, transformer, meter, investigation, stamp, suffix = dataset
    feeder = canonical_feeder(session, transformer.feeder_id)
    assert feeder.transformer_count == feeder.meter_count == 1
    assert feeder.created_at is None
    summary = feeder_summary(session, transformer.feeder_id, canonical_filters(transformer, stamp))
    assert summary.input_energy_source == "DERIVED"
    assert summary.status == "HIGH"
    assert summary.period_start == stamp.replace(tzinfo=UTC)
    trend = feeder_trends(session, transformer.feeder_id, canonical_filters(transformer, stamp))
    assert trend.interval_minutes == 15
    assert len(trend.points) == 4
    topology = feeder_topology(session, transformer.feeder_id)
    assert topology.total == topology.pages == 1
    assert topology.transformers[0].meters[0].meter_id == meter.id
    assert canonical_feeder(session, -1) is None


@pytest.mark.integration
def test_canonical_energy_large_decimal_keeps_all_six_places(dataset):
    from sqlalchemy import select

    from opengrid.read_contracts import canonical_dashboard_summary

    session, transformer, meter, investigation, stamp, suffix = dataset
    balance = session.scalar(select(Balance).where(Balance.asset_id == transformer.id))
    balance.input_kwh = Decimal("12345678901234.123456")
    balance.downstream_kwh = Decimal("10000000000000.000001")
    balance.accounting_difference_kwh = balance.input_kwh - balance.downstream_kwh
    session.flush()
    result = canonical_dashboard_summary(session, canonical_filters(transformer, stamp, 15))
    wire = result.model_dump(mode="json")
    assert wire["input_energy_kwh"] == "12345678901234.123456"
    assert wire["downstream_energy_kwh"] == "10000000000000.000001"
    assert wire["accounting_difference_kwh"] == "2345678901234.123455"


@pytest.mark.integration
def test_canonical_preserves_repeating_meter_baseline_from_recorded_evidence(dataset):
    from sqlalchemy import select

    from opengrid.read_contracts import canonical_dashboard

    session, transformer, meter, investigation, stamp, suffix = dataset
    finding = session.scalar(select(Anomaly).where(Anomaly.asset_id == meter.id))
    recorded_mean = "0.2139081428571428571428571429"
    finding.evidence = {**finding.evidence, "mean_28_days": recorded_mean}
    session.flush()
    result = canonical_dashboard(session, canonical_filters(transformer, stamp))
    assert result.model_dump(mode="json")["top_meters"][0]["baseline_consumption_kwh"] == recorded_mean
