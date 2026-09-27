"""Canonical envelopes plus an explicit reader for released reference events.

Canonical measurement events contain typed measurement payloads. The original
pipeline transports database references; ``LegacyEventEnvelopeContract`` retains
that separate version-1 wire contract instead of reinterpreting references as
measurements. Producers migrate deliberately, not by renaming existing fields.
"""

from enum import StrEnum
from uuid import UUID

from pydantic import Field, JsonValue

from .anomaly import AnomalyContract
from .common import CanonicalContract, UtcTimestamp
from .energy import TransformerEnergyBalanceContract
from .event import EventContract
from .investigation import InvestigationContract
from .meter import MeterReadingContract
from .quality import DataQualityContract
from .transformer import TransformerReadingContract


class CanonicalEventType(StrEnum):
    METER_READING = "meter.reading"
    TRANSFORMER_READING = "transformer.reading"
    METER_EVENT = "meter.event"
    METER_QUALITY = "meter.quality"
    TRANSFORMER_BALANCE = "transformer.balance"
    ANOMALY_CREATED = "anomaly.created"
    INVESTIGATION_CREATED = "investigation.created"
    INVESTIGATION_UPDATED = "investigation.updated"


METER_READING = CanonicalEventType.METER_READING.value
TRANSFORMER_READING = CanonicalEventType.TRANSFORMER_READING.value
METER_QUALITY = CanonicalEventType.METER_QUALITY.value
TRANSFORMER_BALANCE = CanonicalEventType.TRANSFORMER_BALANCE.value
ANOMALY_CREATED = CanonicalEventType.ANOMALY_CREATED.value
INVESTIGATION_CREATED = CanonicalEventType.INVESTIGATION_CREATED.value
INVESTIGATION_UPDATED = CanonicalEventType.INVESTIGATION_UPDATED.value

class KafkaEnvelopeContract[Payload](CanonicalContract):
    event_id: UUID
    event_type: CanonicalEventType
    event_time: UtcTimestamp
    producer: str = Field(min_length=1, max_length=64)
    correlation_id: UUID
    payload: Payload


EVENT_PAYLOADS = {
    CanonicalEventType.METER_READING: MeterReadingContract,
    CanonicalEventType.TRANSFORMER_READING: TransformerReadingContract,
    CanonicalEventType.METER_EVENT: EventContract,
    CanonicalEventType.METER_QUALITY: DataQualityContract,
    CanonicalEventType.TRANSFORMER_BALANCE: TransformerEnergyBalanceContract,
    CanonicalEventType.ANOMALY_CREATED: AnomalyContract,
    CanonicalEventType.INVESTIGATION_CREATED: InvestigationContract,
    CanonicalEventType.INVESTIGATION_UPDATED: InvestigationContract,
}


def parse_canonical_event(data: dict) -> KafkaEnvelopeContract:
    kind = CanonicalEventType(data.get("event_type"))
    return KafkaEnvelopeContract[EVENT_PAYLOADS[kind]].model_validate(data)


class LegacyEventEnvelopeContract(CanonicalContract):
    event_id: UUID
    payload: dict[str, JsonValue]
    event_type: str | None = Field(default=None, max_length=100)
    event_time: UtcTimestamp | None = None
    producer: str | None = Field(default=None, max_length=64)
    correlation_id: UUID | None = None
