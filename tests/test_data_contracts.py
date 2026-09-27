"""Canonical schema fixtures, numeric/time semantics and real Kafka transport."""

import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opengrid.contracts import ReadingInput
from opengrid.contracts.anomaly import AnomalyContract
from opengrid.contracts.common import PaginatedResponse
from opengrid.contracts.dashboard import DashboardFilterContract, DashboardSummaryContract
from opengrid.contracts.energy import (
    AnalyticalEnergyContract,
    EnergyMetricContract,
    MeterIntervalEnergyContract,
    OptionalEnergyContract,
    TransformerEnergyBalanceContract,
)
from opengrid.contracts.envelope import (
    KafkaEnvelopeContract,
    LegacyEventEnvelopeContract,
    parse_canonical_event,
)
from opengrid.contracts.feeder import FeederContract
from opengrid.contracts.generate import END, START, example_contracts
from opengrid.contracts.legacy import ReadingInput as LegacyReadingInput
from opengrid.contracts.meter import MeterReadingContract, import_only_register_adapter

EXAMPLES = example_contracts()
FIXTURES = Path(__file__).resolve().parents[1] / "examples" / "contracts"


@pytest.mark.parametrize("name", list(EXAMPLES))
def test_generated_contract_example_and_schema_match(name):
    example = EXAMPLES[name]
    actual = type(example).model_validate_json((FIXTURES / f"{name}.json").read_text())
    assert actual == example
    schema = json.loads((FIXTURES / "schemas" / f"{name}.schema.json").read_text())
    assert schema == type(example).model_json_schema(mode="serialization")
    assert schema["properties"]["schema_version"]["const"] == 1
    assert schema["additionalProperties"] is False


def meter_payload():
    return EXAMPLES["meter-reading"].model_dump(mode="json")


def test_meter_reading_contract_requires_both_registers_and_precise_decimals():
    payload = meter_payload()
    reading = MeterReadingContract.model_validate(payload)
    assert reading.energy_import_total_kwh == Decimal("12543.842000")
    assert reading.energy_import_total_kwh.as_tuple().exponent == -6
    assert reading.model_dump(mode="json")["energy_import_total_kwh"] == "12543.842000"
    for field in (
        "energy_import_total_kwh",
        "energy_export_total_kwh",
        "received_at",
        "source",
        "meter_id",
    ):
        missing = dict(payload)
        missing.pop(field)
        with pytest.raises(ValidationError):
            MeterReadingContract.model_validate(missing)
    with pytest.raises(ValidationError):
        MeterReadingContract.model_validate(payload | {"energy_export_total_kwh": None})
    with pytest.raises(ValidationError):
        MeterReadingContract.model_validate(payload | {"energy_import_total_kwh": "0.1234567"})


@pytest.mark.parametrize(
    "invalid", ["NaN", "Infinity", "-Infinity", -1, True, "not a number", {}, []]
)
def test_raw_register_contract_rejects_invalid_measurements(invalid):
    with pytest.raises(ValidationError):
        MeterReadingContract.model_validate(meter_payload() | {"energy_import_total_kwh": invalid})


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": "1"},
        {"meter_id": "234"},
        {"meter_id": True},
        {"meter_id": 0},
        {"reading_time": "2026-09-27T10:15:00"},
        {"reading_time": 12345},
        {"reading_time": "2026-09-27T10:16:00Z"},
        {"unknown_field": "ignored?"},
    ],
)
def test_canonical_boundary_rejects_ambiguous_ids_versions_times_and_extra_fields(changes):
    with pytest.raises(ValidationError):
        MeterReadingContract.model_validate(meter_payload() | changes)


def test_timestamps_normalize_utc_and_remain_distinct():
    reading = MeterReadingContract.model_validate(
        meter_payload()
        | {
            "reading_time": "2026-09-27T15:45:00+05:30",
            "processed_at": "2026-09-27T10:15:13Z",
        }
    )
    assert reading.reading_time == datetime(2026, 9, 27, 10, 15, tzinfo=UTC)
    assert reading.received_at != reading.processed_at != reading.reading_time
    assert reading.model_dump(mode="json")["reading_time"].endswith("Z")


def test_import_only_adaptation_is_explicit_and_records_provenance():
    payload = meter_payload()
    payload.pop("energy_export_total_kwh")
    with pytest.raises(ValidationError):
        MeterReadingContract.model_validate(payload)
    converted = import_only_register_adapter(payload, policy_source="measurement_configuration:71")
    assert "energy_export_total_kwh" not in payload
    reading = MeterReadingContract.model_validate(converted)
    assert reading.energy_export_total_kwh == 0
    assert reading.source_metadata["export_register_source"] == "EXPLICIT_IMPORT_ONLY_POLICY"
    with pytest.raises(ValueError):
        import_only_register_adapter(payload, policy_source="")


def analytical(**changes):
    example = EXAMPLES["dashboard-summary"]
    names = AnalyticalEnergyContract.model_fields
    payload = {
        name: value for name, value in example.model_dump(mode="json").items() if name in names
    }
    return AnalyticalEnergyContract.model_validate(payload | changes)


def test_null_zero_partial_and_negative_net_flow_are_distinct():
    positive = analytical()
    assert positive.input_energy_kwh == Decimal("100")
    zero = analytical(
        input_energy_kwh="0",
        downstream_energy_kwh="0",
        accounting_difference_kwh="0",
        accounting_difference_percent=None,
    )
    assert zero.input_energy_kwh == 0 and zero.accounting_difference_percent is None
    missing = analytical(
        input_energy_kwh=None,
        downstream_energy_kwh=None,
        accounting_difference_kwh=None,
        accounting_difference_percent=None,
        input_energy_source="UNAVAILABLE",
        downstream_energy_source="UNAVAILABLE",
        accounting_difference_source="UNAVAILABLE",
        energy_availability="UNAVAILABLE",
    )
    assert missing.input_energy_kwh is None
    upstream_missing = analytical(
        input_energy_kwh=None,
        downstream_energy_kwh="0",
        accounting_difference_kwh=None,
        accounting_difference_percent=None,
        input_energy_source="UNAVAILABLE",
        accounting_difference_source="UNAVAILABLE",
        energy_availability="PARTIAL",
    )
    assert upstream_missing.downstream_energy_kwh == Decimal("0")
    downstream_missing = analytical(
        downstream_energy_kwh=None,
        accounting_difference_kwh=None,
        accounting_difference_percent=None,
        downstream_energy_source="UNAVAILABLE",
        accounting_difference_source="UNAVAILABLE",
        energy_availability="PARTIAL",
    )
    assert downstream_missing.input_energy_kwh == Decimal("100")
    reverse_flow = analytical(
        input_energy_kwh="-10",
        downstream_energy_kwh="-12",
        accounting_difference_kwh="2",
        accounting_difference_percent=None,
    )
    assert reverse_flow.energy_basis == "NET_IMPORT"
    assert reverse_flow.input_energy_kwh == Decimal("-10")


@pytest.mark.parametrize(
    "changes",
    [
        {"input_energy_kwh": None},
        {
            "input_energy_kwh": "0",
            "downstream_energy_kwh": "0",
            "accounting_difference_kwh": "0",
            "accounting_difference_percent": "0",
        },
        {"accounting_difference_kwh": "19"},
        {"accounting_difference_percent": "20"},
        {"input_energy_source": "UNAVAILABLE"},
        {"energy_availability": "UNAVAILABLE"},
    ],
)
def test_balance_contract_rejects_invented_values_and_division_by_zero(changes):
    with pytest.raises(ValidationError):
        analytical(**changes)


def test_partial_coverage_must_not_masquerade_as_complete_balance():
    partial = EnergyMetricContract(
        value_kwh="11580.2",
        source="DERIVED",
        availability="PARTIAL",
        expected_intervals=1000,
        valid_intervals=982,
    )
    assert partial.value_kwh == Decimal("11580.2")
    with pytest.raises(ValidationError):
        EnergyMetricContract(
            value_kwh="11580.2",
            source="DERIVED",
            availability="AVAILABLE",
            expected_intervals=1000,
            valid_intervals=982,
        )
    payload = EXAMPLES["transformer-balance"].model_dump(mode="json")
    with pytest.raises(ValidationError):
        TransformerEnergyBalanceContract.model_validate(payload | {"valid_meter_count": 4})
    partial_balance = TransformerEnergyBalanceContract.model_validate(
        payload
        | {
            "valid_meter_count": 4,
            "data_completeness_percent": "80",
            "energy_availability": "PARTIAL",
            "accounting_difference_kwh": None,
            "accounting_difference_percent": None,
        }
    )
    assert partial_balance.accounting_difference_kwh is None


def test_successful_interval_energy_cannot_represent_a_missing_or_reset_interval():
    payload = EXAMPLES["meter-interval-energy"].model_dump(mode="json")
    valid = MeterIntervalEnergyContract.model_validate(payload)
    assert valid.import_energy_kwh == Decimal("6.320000")
    assert valid.avg_import_power_kw == Decimal("25.28")
    for changes in (
        {"import_energy_kwh": None},
        {"register_status": "RESET"},
        {"multiplier": "0"},
        {"import_energy_kwh": "0"},
        {"avg_import_power_kw": "6.32"},
        {"interval_end": "2026-09-27T10:30:00Z"},
        {"register_status": "ROLLOVER"},
        {"quality": "ESTIMATED"},
    ):
        with pytest.raises(ValidationError):
            MeterIntervalEnergyContract.model_validate(payload | changes)
    missing = EXAMPLES["unavailable-interval-energy"]
    assert missing.import_energy_kwh is None and missing.availability == "UNAVAILABLE"


def test_optional_energy_source_cannot_hide_null_as_zero():
    assert OptionalEnergyContract(energy_kwh="0", source="DERIVED").energy_kwh == Decimal("0")
    assert OptionalEnergyContract(energy_kwh=None, source="UNAVAILABLE").energy_kwh is None
    with pytest.raises(ValidationError):
        OptionalEnergyContract(energy_kwh=None, source="MEASURED")


def test_dashboard_filter_contract_bounds_range_and_point_count():
    assert DashboardFilterContract(from_time=START, to_time=END).granularity == "15m"
    for changes in (
        {"to_time": START},
        {"to_time": "2027-01-01T00:00:00Z"},
        {"to_time": "2026-12-01T00:00:00Z", "granularity": "15m"},
        {"feeder_ids": [-1]},
        {"feeder_ids": [1, 1]},
        {"severity": ["ALIEN"]},
    ):
        with pytest.raises(ValidationError):
            DashboardFilterContract.model_validate({"from_time": START, "to_time": END} | changes)
    assert DashboardFilterContract(
        from_time=START, to_time="2026-12-01T00:00:00Z", granularity="1d"
    )
    with pytest.raises(ValidationError):
        DashboardFilterContract.model_validate(
            {"from_time": START, "to_time": "2026-09-29T10:00:00Z"}, context={"max_range_days": 1}
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"meter_count": -1},
        {"confidence_score": "101"},
        {"data_completeness_percent": "-0.1"},
        {"input_energy_kwh": "Infinity"},
    ],
)
def test_dashboard_summary_contract_rejects_invalid_counts_and_evidence(changes):
    with pytest.raises(ValidationError):
        DashboardSummaryContract.model_validate(
            EXAMPLES["dashboard-summary"].model_dump(mode="json") | changes
        )


def test_anomaly_contract_has_controlled_type_separate_confidence_and_numeric_id():
    payload = EXAMPLES["anomaly"].model_dump(mode="json")
    assert payload["anomaly_id"] == 82 and payload["anomaly_score"] != payload["data_confidence"]
    for changes in ({"anomaly_type": "AI_THEFT"}, {"anomaly_score": "101"}, {"schema_version": 2}):
        with pytest.raises(ValidationError):
            AnomalyContract.model_validate(payload | changes)


def test_api_pagination_contract_includes_empty_state_and_rejects_inconsistent_counts():
    request_id = uuid4()
    empty = PaginatedResponse[FeederContract](
        items=[], page=1, page_size=50, total=0, pages=0, request_id=request_id
    )
    assert empty.items == [] and empty.pages == 0
    with pytest.raises(ValidationError):
        PaginatedResponse[FeederContract](
            items=[], page=1, page_size=50, total=100, pages=1, request_id=request_id
        )


def test_kafka_envelope_contract_round_trip_and_version_rejection():
    envelope = EXAMPLES["kafka-envelope"]
    parsed = parse_canonical_event(json.loads(envelope.model_dump_json()))
    assert parsed == envelope
    assert parsed.correlation_id == envelope.correlation_id
    assert parsed.payload.energy_import_total_kwh == Decimal("12543.842000")
    for changes in ({"schema_version": 2}, {"payload": {"reading_id": 1}}):
        with pytest.raises(ValidationError):
            parse_canonical_event(envelope.model_dump(mode="json") | changes)
    with pytest.raises(ValueError):
        parse_canonical_event(
            envelope.model_dump(mode="json") | {"event_type": "unknown.calculation"}
        )


def test_legacy_ingestion_and_reference_envelopes_remain_explicitly_compatible():
    assert ReadingInput is LegacyReadingInput
    legacy = ReadingInput(
        event_id=uuid4(),
        entity_code="M-1",
        measurement_kind="CUMULATIVE_REGISTER",
        reading_time="2026-01-01T10:00:00Z",
        import_energy_total_kwh="12543.21",
    )
    assert legacy.export_energy_total_kwh is None
    reference = LegacyEventEnvelopeContract(event_id=uuid4(), payload={"reading_id": 1})
    assert reference.payload == {"reading_id": 1} and reference.correlation_id is None
    with pytest.raises(ValidationError):
        LegacyEventEnvelopeContract(event_id=uuid4(), schema_version=2, payload={"reading_id": 1})


@pytest.mark.integration
def test_canonical_kafka_round_trip_preserves_decimal_time_enum_id_and_correlation():
    from confluent_kafka import Consumer, Producer
    from confluent_kafka.admin import AdminClient, NewTopic

    from opengrid.config import settings

    bootstrap = settings().kafka_bootstrap_servers
    topic = "opengrid-contract-test-" + uuid4().hex
    admin = AdminClient({"bootstrap.servers": bootstrap})
    admin.create_topics([NewTopic(topic, 1, 1)])[topic].result(timeout=30)
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": uuid4().hex,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    try:
        expected = EXAMPLES["kafka-envelope"]
        producer = Producer({"bootstrap.servers": bootstrap, "enable.idempotence": True})
        errors = []
        producer.produce(
            topic,
            key=str(expected.payload.meter_id),
            value=expected.model_dump_json().encode(),
            on_delivery=lambda error, message: errors.append(error) if error else None,
        )
        assert producer.flush(30) == 0 and not errors
        consumer.subscribe([topic])
        deadline = time.monotonic() + 30
        message = None
        while time.monotonic() < deadline:
            message = consumer.poll(1)
            if message is not None:
                assert not message.error(), str(message.error())
                break
        assert message is not None, "canonical Kafka message was not received"
        actual = KafkaEnvelopeContract[MeterReadingContract].model_validate_json(message.value())
        assert actual == expected
        assert actual.event_time.tzinfo == UTC
        assert actual.payload.energy_import_total_kwh.as_tuple().exponent == -6
    finally:
        consumer.close()
        admin.delete_topics([topic])[topic].result(timeout=30)
