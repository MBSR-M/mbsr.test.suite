"""Anomaly evidence is explanatory accounting evidence, never an allegation."""

from enum import StrEnum

from pydantic import Field, JsonValue, model_validator

from .common import (
    CanonicalContract,
    Count,
    EntityType,
    Fraction,
    Percent,
    PositiveId,
    Severity,
    SignedPercent,
    UtcTimestamp,
)


class AnomalyType(StrEnum):
    TRANSFORMER_IMBALANCE = "TRANSFORMER_IMBALANCE"
    TRANSFORMER_DATA_QUALITY = "TRANSFORMER_DATA_QUALITY"
    TRANSFORMER_COMMUNICATION = "TRANSFORMER_COMMUNICATION"
    CONSUMPTION_DROP = "CONSUMPTION_DROP"
    CONSUMPTION_SPIKE = "CONSUMPTION_SPIKE"
    FLATLINE = "FLATLINE"
    ZERO_CONSUMPTION = "ZERO_CONSUMPTION"
    METER_DATA_QUALITY = "METER_DATA_QUALITY"
    METER_COMMUNICATION = "METER_COMMUNICATION"


class ScoreComponentsContract(CanonicalContract):
    loss_deviation: Fraction | None = None
    persistence: Fraction | None = None
    data_completeness: Fraction | None = None
    consumption_anomaly: Fraction | None = None
    event_correlation: Fraction | None = None


class AnomalyEvidenceContract(CanonicalContract):
    current_imbalance_percent: SignedPercent | None = None
    baseline_imbalance_percent: SignedPercent | None = None
    deviation_percentage_points: SignedPercent | None = None
    persistent_intervals: Count | None = None
    data_completeness_percent: Percent | None = None
    affected_meter_count: Count | None = None
    communication_failures: Count | None = None
    relevant_event_count: Count | None = None
    components: ScoreComponentsContract | None = None
    details: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Original machine-readable engine evidence, retained without invention.",
    )


class AnomalyContract(CanonicalContract):
    anomaly_id: PositiveId
    entity_type: EntityType
    entity_id: PositiveId
    anomaly_type: AnomalyType
    interval_start: UtcTimestamp
    detected_at: UtcTimestamp | None = Field(
        default=None,
        description="Null when legacy storage did not retain detection processing time.",
    )
    first_seen: UtcTimestamp
    last_seen: UtcTimestamp
    severity: Severity
    anomaly_score: Percent
    data_confidence: Percent | None
    occurrence_count: Count
    evidence: AnomalyEvidenceContract
    status: str = Field(pattern=r"^(OPEN|ACKNOWLEDGED|RESOLVED|DISMISSED|SUPERSEDED)$")

    @model_validator(mode="after")
    def ordered_observation(self):
        if self.last_seen < self.first_seen:
            raise ValueError("last_seen cannot precede first_seen")
        if self.occurrence_count == 0:
            raise ValueError("an anomaly requires an observed occurrence")
        return self
