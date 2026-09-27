"""Bounded authoritative operator dashboard read-model contracts."""

from datetime import timedelta
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .anomaly import AnomalyType
from .common import (
    CanonicalContract,
    Code,
    Count,
    DecimalValue,
    Energy,
    EntityType,
    Percent,
    PeriodContract,
    PositiveId,
    Severity,
    SignedEnergy,
    SignedPercent,
    UtcTimestamp,
)
from .energy import AnalyticalEnergyContract
from .investigation import InvestigationStatus


class DashboardFilterContract(CanonicalContract):
    from_time: UtcTimestamp
    to_time: UtcTimestamp
    feeder_ids: list[PositiveId] = Field(default_factory=list, max_length=100)
    transformer_ids: list[PositiveId] = Field(default_factory=list, max_length=100)
    severity: list[Severity] = Field(default_factory=list, max_length=4)
    granularity: Literal["15m", "1h", "1d"] = "15m"

    @model_validator(mode="after")
    def bounded_filter(self, info):
        duration = self.to_time - self.from_time
        maximum_days = (info.context or {}).get("max_range_days", 90)
        if not timedelta(0) < duration <= timedelta(days=maximum_days):
            raise ValueError(f"range must be positive and at most {maximum_days} days")
        minutes = {"15m": 15, "1h": 60, "1d": 1440}[self.granularity]
        if duration.total_seconds() / (minutes * 60) > 3000:
            raise ValueError("choose a coarser granularity; trend is limited to 3000 points")
        if any(
            len(values) != len(set(values))
            for values in (self.feeder_ids, self.transformer_ids, self.severity)
        ):
            raise ValueError("filters cannot contain duplicate identifiers or severities")
        return self


class DashboardSummaryContract(AnalyticalEnergyContract, PeriodContract):
    transformer_count: Count
    meter_count: Count
    feeder_count: Count
    data_completeness_percent: Percent | None
    open_investigation_count: Count
    critical_anomaly_count: Count
    high_anomaly_count: Count
    watch_anomaly_count: Count
    healthy_transformer_count: Count
    watch_transformer_count: Count
    high_transformer_count: Count
    critical_transformer_count: Count
    unknown_transformer_count: Count = 0
    confidence_score: Percent | None


class DashboardTrendPointContract(AnalyticalEnergyContract):
    timestamp: UtcTimestamp
    data_completeness_percent: Percent | None
    anomaly_count: Count


class DashboardTrendContract(PeriodContract):
    granularity: Literal["15m", "1h", "1d"]
    points: list[DashboardTrendPointContract] = Field(max_length=3000)

    @model_validator(mode="after")
    def ordered_points(self):
        times = [point.timestamp for point in self.points]
        if times != sorted(set(times)):
            raise ValueError("trend timestamps must be unique and ascending")
        if any(not self.period_start <= value < self.period_end for value in times):
            raise ValueError("trend point lies outside its declared half-open period")
        return self


class DashboardHealthDistributionContract(CanonicalContract):
    healthy: Count
    watch: Count
    high: Count
    critical: Count
    unknown: Count = 0


class DashboardTransformerItemContract(CanonicalContract):
    transformer_id: PositiveId
    transformer_code: Code
    feeder_id: PositiveId | None
    feeder_code: Code | None
    current_imbalance_percent: SignedPercent | None
    baseline_imbalance_percent: SignedPercent | None
    deviation_percentage_points: SignedPercent | None
    accounting_difference_kwh: SignedEnergy | None
    persistence_intervals: Count | None
    data_completeness_percent: Percent | None
    confidence_score: Percent | None
    anomaly_score: Percent | None
    severity: Severity
    open_investigation_count: Count


class DashboardMeterItemContract(CanonicalContract):
    meter_id: PositiveId
    meter_no: Code
    transformer_id: PositiveId | None
    transformer_code: Code | None
    anomaly_type: AnomalyType
    current_consumption_kwh: Energy | None
    baseline_consumption_kwh: Annotated[DecimalValue, Field(ge=0)] | None
    deviation_percent: SignedPercent | None
    data_completeness_percent: Percent | None
    anomaly_score: Percent
    severity: Severity
    last_reading_time: UtcTimestamp | None


class DashboardInvestigationItemContract(CanonicalContract):
    investigation_id: PositiveId
    case_no: str = Field(min_length=1, max_length=40)
    entity_type: EntityType
    entity_id: PositiveId
    entity_display_name: str = Field(min_length=1, max_length=255)
    priority_score: Percent
    severity: Severity
    estimated_unaccounted_kwh: SignedEnergy | None
    status: InvestigationStatus
    assigned_to: str | None = Field(max_length=128)
    opened_at: UtcTimestamp


class DashboardDataQualityContract(PeriodContract):
    completeness_percent: Percent | None
    expected_intervals: Count | None
    received_intervals: Count | None
    missing_intervals: Count | None
    duplicate_intervals: Count | None
    late_intervals: Count | None
    invalid_intervals: Count | None
    affected_meter_count: Count | None
    affected_transformer_count: Count | None
    availability_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def consistent_counts(self):
        if self.expected_intervals is not None and self.received_intervals is not None:
            if self.received_intervals > self.expected_intervals:
                raise ValueError("received intervals exceed expected intervals")
        return self


class DashboardActivityContract(CanonicalContract):
    activity_id: str = Field(
        min_length=1,
        max_length=80,
        description="Stable source-qualified identifier, e.g. anomaly:42, audit:3 or UUID.",
    )
    timestamp: UtcTimestamp
    activity_type: Literal[
        "READING", "QUALITY", "ANOMALY", "COMMUNICATION", "INVESTIGATION", "SYSTEM"
    ]
    entity_type: str | None = Field(max_length=32)
    entity_id: PositiveId | None
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(max_length=2000)
    severity: Literal["INFO", "WARNING", "HIGH", "CRITICAL"]


class DashboardContract(CanonicalContract):
    generated_at: UtcTimestamp
    filters: DashboardFilterContract
    summary: DashboardSummaryContract
    trend: DashboardTrendContract
    health: DashboardHealthDistributionContract
    top_transformers: list[DashboardTransformerItemContract] = Field(max_length=50)
    top_meters: list[DashboardMeterItemContract] = Field(max_length=50)
    top_investigations: list[DashboardInvestigationItemContract] = Field(max_length=50)
    data_quality: DashboardDataQualityContract
    recent_activity: list[DashboardActivityContract] = Field(max_length=100)

    @model_validator(mode="after")
    def shared_period(self):
        for section in (self.summary, self.trend, self.data_quality):
            if (section.period_start, section.period_end) != (
                self.filters.from_time,
                self.filters.to_time,
            ):
                raise ValueError("all dashboard sections must use the requested period")
        return self
