"""Generate validated canonical examples and JSON schemas deterministically.

Run ``python -m opengrid.contracts.generate --output examples/contracts``.
The example values illustrate semantics; they are not claims about live data.
"""

import argparse
import json
from pathlib import Path

from .anomaly import AnomalyContract, AnomalyEvidenceContract, ScoreComponentsContract
from .common import PaginatedResponse, SuccessResponse
from .dashboard import (
    DashboardActivityContract,
    DashboardContract,
    DashboardDataQualityContract,
    DashboardFilterContract,
    DashboardHealthDistributionContract,
    DashboardInvestigationItemContract,
    DashboardMeterItemContract,
    DashboardSummaryContract,
    DashboardTransformerItemContract,
    DashboardTrendContract,
    DashboardTrendPointContract,
)
from .energy import (
    EnergyMetricContract,
    MeterIntervalEnergyContract,
    OptionalEnergyContract,
    RegisterDeltaContract,
    TransformerEnergyBalanceContract,
    UnavailableIntervalEnergyContract,
)
from .envelope import KafkaEnvelopeContract
from .errors import ErrorResponse
from .event import EventContract
from .feeder import (
    FeederContract,
    FeederSummaryContract,
    FeederTopologyContract,
    FeederTrendContract,
    FeederTrendPointContract,
    MeterTopologyContract,
    TransformerTopologyContract,
)
from .investigation import AuditEventContract, InvestigationContract, InvestigationNoteContract
from .meter import MeterContract, MeterReadingContract
from .quality import DataQualityContract
from .transformer import TransformerContract, TransformerReadingContract

START = "2026-09-27T10:00:00Z"
END = "2026-09-27T10:15:00Z"
ID = "724b3e6f-8db5-45ce-bbb0-20eb0e30f166"
CORRELATION = "d1c2a096-64f0-4fd0-8742-1ab1328f7948"


def example_contracts() -> dict:
    period = {"period_start": START, "period_end": END}
    interval = {"interval_start": START, "interval_end": END, "interval_minutes": 15}
    energy = {
        "input_energy_kwh": "100",
        "downstream_energy_kwh": "81.3",
        "accounting_difference_kwh": "18.7",
        "accounting_difference_percent": "18.7",
        "input_energy_source": "DERIVED",
        "downstream_energy_source": "DERIVED",
        "accounting_difference_source": "DERIVED",
        "energy_availability": "AVAILABLE",
    }
    register = {
        "event_id": ID,
        "reading_time": END,
        "received_at": "2026-09-27T10:15:12Z",
        "energy_import_total_kwh": "12543.842000",
        "energy_export_total_kwh": "0.000000",
        "source": "AMI",
        "register_epoch": "installation-1",
        "voltage_avg_v": "230.120",
    }
    meter_reading = MeterReadingContract(**register, meter_id=234, meter_no="M100234")
    transformer_reading = TransformerReadingContract(
        **register, transformer_id=47, transformer_code="DT-1047"
    )
    evidence = AnomalyEvidenceContract(
        current_imbalance_percent="18.7",
        baseline_imbalance_percent="7.2",
        deviation_percentage_points="11.5",
        persistent_intervals=4,
        data_completeness_percent="100",
        affected_meter_count=1,
        relevant_event_count=2,
        communication_failures=0,
        components=ScoreComponentsContract(
            loss_deviation="0.575",
            persistence="0.666667",
            data_completeness="1",
            consumption_anomaly="0.2",
            event_correlation="0.666667",
        ),
        details={"intervals_required": 4, "window_intervals": 6},
    )
    anomaly = AnomalyContract(
        anomaly_id=82,
        entity_type="TRANSFORMER",
        entity_id=47,
        anomaly_type="TRANSFORMER_IMBALANCE",
        interval_start=START,
        detected_at=END,
        first_seen=START,
        last_seen=START,
        severity="HIGH",
        anomaly_score="72.5",
        data_confidence="100",
        occurrence_count=1,
        evidence=evidence,
        status="OPEN",
    )
    investigation = InvestigationContract(
        investigation_id=182,
        case_no="CASE-2026-00182",
        entity_type="TRANSFORMER",
        entity_id=47,
        anomaly_id=82,
        priority_score="72.5",
        estimated_unaccounted_kwh="18.7",
        status="INVESTIGATING",
        assigned_to="investigator",
        opened_at=END,
        closed_at=None,
        resolution=None,
        version=3,
        evidence=evidence,
    )
    feeder = FeederContract(
        feeder_id=12,
        feeder_code="FDR-12",
        name="Industrial Feeder 12",
        substation=None,
        transformer_count=1,
        meter_count=5,
        created_at=None,
        updated_at=None,
        metadata_unavailable_reason="Creation/update and substation metadata are not stored in legacy MVP.",
    )
    feeder_summary = FeederSummaryContract(
        **period,
        **energy,
        feeder_id=12,
        feeder_code="FDR-12",
        name=feeder.name,
        substation=None,
        transformer_count=1,
        meter_count=5,
        data_completeness_percent="100",
        anomaly_count=1,
        high_anomaly_count=1,
        critical_anomaly_count=0,
        open_investigation_count=1,
        confidence_score="100",
        status="HIGH",
    )
    point = {**energy, "timestamp": START, "data_completeness_percent": "100", "anomaly_count": 1}
    feeder_point = FeederTrendPointContract(**point)
    meter_topology = MeterTopologyContract(
        meter_id=234, meter_no="M100234", consumer_type=None, status="HIGH"
    )
    transformer_topology = TransformerTopologyContract(
        transformer_id=47,
        transformer_code="DT-1047",
        name="Transformer 1047",
        meter_count=5,
        status="HIGH",
        meters=[meter_topology],
        meters_truncated=True,
    )
    filters = DashboardFilterContract(from_time=START, to_time=END, granularity="15m")
    summary = DashboardSummaryContract(
        **period,
        **energy,
        transformer_count=1,
        meter_count=5,
        feeder_count=1,
        data_completeness_percent="100",
        open_investigation_count=1,
        critical_anomaly_count=0,
        high_anomaly_count=1,
        watch_anomaly_count=0,
        healthy_transformer_count=0,
        watch_transformer_count=0,
        high_transformer_count=1,
        critical_transformer_count=0,
        confidence_score="100",
    )
    trend_point = DashboardTrendPointContract(**point)
    trend = DashboardTrendContract(**period, granularity="15m", points=[trend_point])
    health = DashboardHealthDistributionContract(healthy=0, watch=0, high=1, critical=0)
    transformer_item = DashboardTransformerItemContract(
        transformer_id=47,
        transformer_code="DT-1047",
        feeder_id=12,
        feeder_code="FDR-12",
        current_imbalance_percent="18.7",
        baseline_imbalance_percent="7.2",
        deviation_percentage_points="11.5",
        accounting_difference_kwh="18.7",
        persistence_intervals=4,
        data_completeness_percent="100",
        confidence_score="100",
        anomaly_score="72.5",
        severity="HIGH",
        open_investigation_count=1,
    )
    meter_item = DashboardMeterItemContract(
        meter_id=234,
        meter_no="M100234",
        transformer_id=47,
        transformer_code="DT-1047",
        anomaly_type="CONSUMPTION_DROP",
        current_consumption_kwh="0.31",
        baseline_consumption_kwh="1",
        deviation_percent="-69",
        data_completeness_percent="100",
        anomaly_score="70",
        severity="HIGH",
        last_reading_time=END,
    )
    investigation_item = DashboardInvestigationItemContract(
        investigation_id=182,
        case_no=investigation.case_no,
        entity_type="TRANSFORMER",
        entity_id=47,
        entity_display_name="DT-1047",
        priority_score="72.5",
        severity="HIGH",
        estimated_unaccounted_kwh="18.7",
        status="INVESTIGATING",
        assigned_to="investigator",
        opened_at=END,
    )
    data_quality = DashboardDataQualityContract(
        **period,
        completeness_percent="100",
        expected_intervals=5,
        received_intervals=5,
        missing_intervals=0,
        duplicate_intervals=None,
        late_intervals=0,
        invalid_intervals=0,
        affected_meter_count=0,
        affected_transformer_count=0,
        availability_reason="Duplicate-retry counts are not persisted by the legacy ingestion endpoint.",
    )
    activity = DashboardActivityContract(
        activity_id="anomaly:82",
        timestamp=START,
        activity_type="ANOMALY",
        entity_type="TRANSFORMER",
        entity_id=47,
        title="Persistent transformer imbalance",
        description="18.7% vs 7.2% baseline.",
        severity="HIGH",
    )
    results = {
        "register-delta": RegisterDeltaContract(previous_register_kwh="12543.210", current_register_kwh="12543.842", raw_delta_kwh="0.632", meter_multiplier="10", calculated_energy_kwh="6.320", register_status="NORMAL"),
        "meter": MeterContract(meter_id=234, meter_no="M100234", transformer_id=47),
        "transformer": TransformerContract(
            transformer_id=47, transformer_code="DT-1047", feeder_id=12, meter_count=5
        ),
        "meter-reading": meter_reading,
        "transformer-reading": transformer_reading,
        "meter-interval-energy": MeterIntervalEnergyContract(
            **interval,
            meter_id=234,
            import_energy_kwh="6.320000",
            export_energy_kwh="0",
            import_delta_kwh="0.632000",
            export_delta_kwh="0",
            multiplier="10",
            register_status="NORMAL",
            quality="VALID",
            avg_import_power_kw="25.28",
        ),
        "unavailable-interval-energy": UnavailableIntervalEnergyContract(
            **interval,
            entity_id=234,
            entity_type="METER",
            calculation_status="MISSING_START",
            unavailable_reason="Previous register boundary is unavailable.",
        ),
        "optional-energy": OptionalEnergyContract(
            energy_kwh=None,
            source="UNAVAILABLE",
            unavailable_reason="No validated upstream measurement.",
        ),
        "energy-metric": EnergyMetricContract(
            value_kwh="81.3",
            availability="PARTIAL",
            source="DERIVED",
            expected_intervals=1000,
            valid_intervals=982,
        ),
        "transformer-balance": TransformerEnergyBalanceContract(
            **interval,
            transformer_id=47,
            transformer_input_energy_kwh="100",
            downstream_meter_energy_kwh="81.3",
            accounting_difference_kwh="18.7",
            accounting_difference_percent="18.7",
            expected_meter_count=5,
            valid_meter_count=5,
            data_completeness_percent="100",
            confidence_score="100",
            energy_availability="AVAILABLE",
        ),
        "data-quality": DataQualityContract(
            **interval,
            entity_id=234,
            entity_type="METER",
            expected=True,
            received=True,
            quality="VALID",
            delay_seconds=12,
        ),
        "anomaly": anomaly,
        "anomaly-evidence": evidence,
        "score-components": evidence.components,
        "investigation": investigation,
        "investigation-note": InvestigationNoteContract(
            note_id=1,
            investigation_id=182,
            author="investigator",
            note="Check CT wiring during field visit.",
            created_at=END,
        ),
        "audit-event": AuditEventContract(
            audit_id=1,
            actor="user:investigator",
            action="INVESTIGATION_STATUS_CHANGED",
            entity_type="INVESTIGATION",
            entity_id=182,
            timestamp=END,
            old_value={"status": "ASSIGNED"},
            new_value={"status": "INVESTIGATING"},
            request_id=ID,
        ),
        "event": EventContract(
            event_id=ID,
            entity_id=234,
            entity_type="METER",
            event_time=START,
            event_type="POWER_FAILURE",
            source="AMI",
            evidence={"cause": "source reported"},
        ),
        "kafka-envelope": KafkaEnvelopeContract[MeterReadingContract](
            event_id=ID,
            event_type="meter.reading",
            event_time=END,
            producer="ingestion-service",
            correlation_id=CORRELATION,
            payload=meter_reading,
        ),
        "api-error": ErrorResponse(
            error={
                "code": "METER_NOT_FOUND",
                "message": "Meter does not exist",
                "details": {},
                "request_id": ID,
            }
        ),
        "api-pagination": PaginatedResponse[FeederContract](
            items=[feeder], page=1, page_size=50, total=1, pages=1, request_id=ID
        ),
        "feeder": feeder,
        "feeder-summary": feeder_summary,
        "feeder-trend-point": feeder_point,
        "feeder-trend": FeederTrendContract(
            **period, feeder_id=12, interval_minutes=15, points=[feeder_point]
        ),
        "meter-topology": meter_topology,
        "transformer-topology": transformer_topology,
        "feeder-topology": FeederTopologyContract(
            feeder=feeder,
            transformers=[transformer_topology],
            page=1,
            page_size=20,
            total=1,
            pages=1,
        ),
        "dashboard-filter": filters,
        "dashboard-summary": summary,
        "dashboard-trend-point": trend_point,
        "dashboard-trend": trend,
        "dashboard-health": health,
        "dashboard-transformer": transformer_item,
        "dashboard-meter": meter_item,
        "dashboard-investigation": investigation_item,
        "dashboard-data-quality": data_quality,
        "dashboard-activity": activity,
        "dashboard": DashboardContract(
            generated_at=END,
            filters=filters,
            summary=summary,
            trend=trend,
            health=health,
            top_transformers=[transformer_item],
            top_meters=[meter_item],
            top_investigations=[investigation_item],
            data_quality=data_quality,
            recent_activity=[activity],
        ),
    }
    results["api-success"] = SuccessResponse[DashboardContract](
        data=results["dashboard"], request_id=ID
    )
    return results


def generate(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    schemas = output / "schemas"
    schemas.mkdir(exist_ok=True)
    manifest = {}
    for name, model in example_contracts().items():
        # Round-trip through the actual validator before writing any example.
        type(model).model_validate_json(model.model_dump_json())
        (output / f"{name}.json").write_text(
            model.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        (schemas / f"{name}.schema.json").write_text(
            json.dumps(type(model).model_json_schema(mode="serialization"), indent=2) + "\n",
            encoding="utf-8",
        )
        manifest[name] = type(model).__name__
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("examples/contracts"))
    args = parser.parse_args()
    generate(args.output)
    print(f"Generated {len(example_contracts())} validated examples and schemas in {args.output}.")


if __name__ == "__main__":
    main()
