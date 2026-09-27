"""Investigation identity remains numeric for backward compatibility."""

from enum import StrEnum
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from .anomaly import AnomalyEvidenceContract
from .common import CanonicalContract, EntityType, Percent, PositiveId, SignedEnergy, UtcTimestamp


class InvestigationStatus(StrEnum):
    NEW = "NEW"
    ASSIGNED = "ASSIGNED"
    INVESTIGATING = "INVESTIGATING"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class InvestigationContract(CanonicalContract):
    investigation_id: PositiveId
    case_no: str = Field(min_length=1, max_length=40)
    entity_type: EntityType
    entity_id: PositiveId
    anomaly_id: PositiveId
    priority_score: Percent
    estimated_unaccounted_kwh: SignedEnergy | None
    status: InvestigationStatus
    assigned_to: str | None = Field(max_length=128)
    opened_at: UtcTimestamp
    closed_at: UtcTimestamp | None
    resolution: str | None = Field(max_length=500)
    version: PositiveId
    evidence: AnomalyEvidenceContract

    @model_validator(mode="after")
    def case_lifecycle(self):
        closed = self.status in {InvestigationStatus.RESOLVED, InvestigationStatus.DISMISSED}
        if closed and (self.closed_at is None or not self.resolution):
            raise ValueError("closed cases require closure time and resolution")
        if not closed and self.closed_at is not None:
            raise ValueError("open cases cannot have a closure timestamp")
        if self.closed_at and self.closed_at < self.opened_at:
            raise ValueError("closure cannot precede case opening")
        return self


class InvestigationNoteContract(CanonicalContract):
    note_id: PositiveId
    investigation_id: PositiveId
    author: str = Field(min_length=1, max_length=128)
    note: str = Field(min_length=1, max_length=10000)
    created_at: UtcTimestamp


class AuditEventContract(CanonicalContract):
    audit_id: PositiveId
    actor: str = Field(min_length=1, max_length=128)
    action: str = Field(min_length=1, max_length=64)
    entity_type: str = Field(min_length=1, max_length=32)
    entity_id: PositiveId
    timestamp: UtcTimestamp
    old_value: dict[str, JsonValue] | None
    new_value: dict[str, JsonValue] | None
    request_id: UUID | None = None
