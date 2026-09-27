"""Source meter events and their bounded evidence."""

from enum import StrEnum
from uuid import UUID

from pydantic import Field, JsonValue

from .common import CanonicalContract, EntityType, PositiveId, UtcTimestamp


class MeterEventType(StrEnum):
    POWER_FAILURE = "POWER_FAILURE"
    POWER_RESTORED = "POWER_RESTORED"
    COMMUNICATION_FAILURE = "COMMUNICATION_FAILURE"
    COMMUNICATION_RESTORED = "COMMUNICATION_RESTORED"
    REGISTER_RESET = "REGISTER_RESET"
    COVER_OPEN = "COVER_OPEN"
    MAGNETIC_FIELD = "MAGNETIC_FIELD"


class EventContract(CanonicalContract):
    event_id: UUID
    entity_id: PositiveId
    entity_type: EntityType
    event_time: UtcTimestamp
    event_type: MeterEventType
    source: str = Field(min_length=1, max_length=64)
    evidence: dict[str, JsonValue] = Field(default_factory=dict)
