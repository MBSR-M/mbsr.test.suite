from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadingInput(Contract):
    event_id: UUID
    schema_version: Literal[1] = 1
    entity_code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    measurement_kind: Literal["CUMULATIVE_REGISTER", "INTERVAL_ENERGY"]
    reading_time: datetime
    interval_start: datetime | None = None
    import_energy_total_kwh: Decimal | None = Field(
        default=None, ge=0, max_digits=20, decimal_places=6
    )
    export_energy_total_kwh: Decimal | None = Field(
        default=None, ge=0, max_digits=20, decimal_places=6
    )
    import_energy_kwh: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=6)
    export_energy_kwh: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=6)
    source: str = Field(default="AMI", min_length=1, max_length=32)
    revision: int = Field(default=1, ge=1)
    register_epoch: str = Field(default="installation-1", max_length=64)
    energy_basis: Literal["PRIMARY", "REGISTER_SECONDARY"] = "REGISTER_SECONDARY"
    received_at: datetime | None = None
    rollover_confirmed: bool = False
    voltage_avg_v: Decimal | None = Field(default=None, ge=0, le=100000)
    current_avg_a: Decimal | None = Field(default=None, ge=0, le=100000)
    frequency_avg_hz: Decimal | None = Field(default=None, ge=0, le=1000)

    @field_validator("reading_time", "interval_start", "received_at")
    @classmethod
    def aware(cls, value):
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timestamps must include a UTC offset")
            return value.astimezone(UTC)
        return value

    @model_validator(mode="after")
    def semantics(self):
        t = self.reading_time
        if t.minute % 15 or t.second or t.microsecond:
            raise ValueError("MVP requires aligned 15-minute boundaries")
        if t > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("reading is too far in the future")
        if self.measurement_kind == "CUMULATIVE_REGISTER":
            if (
                self.import_energy_total_kwh is None
                or self.import_energy_kwh is not None
                or self.export_energy_kwh is not None
                or self.interval_start is not None
            ):
                raise ValueError("register readings require total energy only")
        else:
            if (
                self.import_energy_kwh is None
                or self.import_energy_total_kwh is not None
                or self.export_energy_total_kwh is not None
                or self.interval_start != t - timedelta(minutes=15)
            ):
                raise ValueError(
                    "interval energy requires exact start/end and interval energy only"
                )
        return self


class AssetInput(Contract):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(default="", max_length=255)
    feeder_id: int | None = None
    transformer_id: int | None = None
    multiplier: Decimal = Field(default=Decimal(1), gt=0, max_digits=20, decimal_places=6)
    import_only: bool = True
    max_power_kw: Decimal | None = Field(default=None, gt=0)
    modulus: Decimal | None = Field(default=None, gt=0)
    valid_from: datetime

    @field_validator("valid_from")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("UTC offset required")
        value = value.astimezone(UTC)
        if value.minute % 15 or value.second or value.microsecond:
            raise ValueError("configuration must start on an interval boundary")
        return value


class CaseUpdate(Contract):
    version: int = Field(ge=1)
    status: Literal["NEW", "ASSIGNED", "INVESTIGATING", "RESOLVED", "DISMISSED"]
    assigned_to: str | None = Field(default=None, max_length=128)
    resolution: str | None = Field(default=None, max_length=500)


class ReprocessInput(Contract):
    transformer_id: int
    start_time: datetime
    end_time: datetime

    @model_validator(mode="after")
    def bounds(self):
        if self.start_time.tzinfo is None or self.end_time.tzinfo is None:
            raise ValueError("UTC offsets required")
        self.start_time = self.start_time.astimezone(UTC)
        self.end_time = self.end_time.astimezone(UTC)
        if not timedelta(0) < self.end_time - self.start_time <= timedelta(days=31):
            raise ValueError("range must be positive and at most 31 days")
        if any(
            t.minute % 15 or t.second or t.microsecond for t in (self.start_time, self.end_time)
        ):
            raise ValueError("aligned boundaries required")
        return self


class AssignmentInput(Contract):
    transformer_id: int
    valid_from: datetime

    @field_validator("valid_from")
    @classmethod
    def boundary(cls, value):
        if value.tzinfo is None:
            raise ValueError("UTC offset required")
        value = value.astimezone(UTC)
        if value.minute % 15 or value.second or value.microsecond:
            raise ValueError("assignment requires aligned boundary")
        return value


class EventInput(Contract):
    event_id: UUID
    meter_id: int
    event_time: datetime
    event_type: Literal[
        "POWER_FAILURE",
        "POWER_RESTORED",
        "COMMUNICATION_FAILURE",
        "COMMUNICATION_RESTORED",
        "REGISTER_RESET",
        "COVER_OPEN",
        "MAGNETIC_FIELD",
    ]
    evidence: dict[str, str] = Field(default_factory=dict)

    @field_validator("event_time")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("UTC offset required")
        return value.astimezone(UTC)
