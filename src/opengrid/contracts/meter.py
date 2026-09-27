"""Meter metadata and measured cumulative registers."""

from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .common import (
    BoundedText,
    CanonicalContract,
    Code,
    DecimalValue,
    Energy,
    PositiveId,
    UtcTimestamp,
)


class MeterContract(CanonicalContract):
    meter_id: PositiveId
    meter_no: Code
    transformer_id: PositiveId | None
    name: str | None = Field(default=None, max_length=255)
    consumer_type: str | None = Field(default=None, max_length=64)
    phase: Literal["SINGLE_PHASE", "THREE_PHASE"] | None = None
    sanctioned_load_kw: Annotated[DecimalValue, Field(ge=0)] | None = None
    active: Annotated[bool, Field(strict=True)] = True


class RegisterReadingContract(CanonicalContract):
    event_id: UUID
    measurement_kind: Literal["CUMULATIVE_REGISTER"] = "CUMULATIVE_REGISTER"
    reading_time: UtcTimestamp = Field(
        description="Time the device measured the register, not ingestion time."
    )
    received_at: UtcTimestamp = Field(description="Time OpenGrid/HES received the measurement.")
    processed_at: UtcTimestamp | None = Field(
        default=None, description="Successful processing time; null before processing."
    )
    energy_import_total_kwh: Energy
    energy_export_total_kwh: Energy
    import_power_kw: Annotated[DecimalValue, Field(ge=0)] | None = None
    export_power_kw: Annotated[DecimalValue, Field(ge=0)] | None = None
    voltage_avg_v: Annotated[DecimalValue, Field(ge=0, le=100000)] | None = None
    current_avg_a: Annotated[DecimalValue, Field(ge=0, le=100000)] | None = None
    frequency_avg_hz: Annotated[DecimalValue, Field(ge=0, le=1000)] | None = None
    source: BoundedText
    source_metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Adapter provenance, including any explicit import-only export-register transformation.",
    )
    energy_basis: Literal["PRIMARY", "REGISTER_SECONDARY"] = "REGISTER_SECONDARY"
    register_epoch: str = Field(min_length=1, max_length=64)
    revision: Annotated[int, Field(strict=True, ge=1)] = 1

    @model_validator(mode="after")
    def reading_semantics(self):
        if (
            self.reading_time.minute % 15
            or self.reading_time.second
            or self.reading_time.microsecond
        ):
            raise ValueError("MVP register readings require UTC quarter-hour boundaries")
        if self.received_at + timedelta(minutes=5) < self.reading_time:
            raise ValueError("reading time cannot exceed reception time by more than five minutes")
        if self.processed_at and self.processed_at < self.received_at:
            raise ValueError("processing cannot precede reception")
        return self


class MeterReadingContract(RegisterReadingContract):
    meter_id: PositiveId
    meter_no: Code | None = None


def import_only_register_adapter(payload: dict, *, policy_source: str) -> dict:
    """Explicit ingress adaptation, never used implicitly by model validation.

    Caller must have confirmed an import-only device configuration. The source
    policy is recorded so synthetic zero export cannot masquerade as measured.
    """
    if not policy_source.strip():
        raise ValueError("documented import-only policy source required")
    converted = dict(payload)
    if converted.get("energy_export_total_kwh") is None:
        converted["energy_export_total_kwh"] = "0"
        converted["source_metadata"] = {
            **converted.get("source_metadata", {}),
            "export_register_source": "EXPLICIT_IMPORT_ONLY_POLICY",
            "export_register_policy": policy_source,
        }
    return converted
