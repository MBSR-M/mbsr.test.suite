"""Common canonical wire rules (schema version 1), independent of persistence."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)


def utc_timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("ISO-8601 timestamp required") from error
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return value.astimezone(UTC)


def finite_decimal(value):
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise ValueError("finite decimal value required")
    try:
        number = Decimal(str(value))
    except Exception as error:
        raise ValueError("finite decimal value required") from error
    if not number.is_finite():
        raise ValueError("NaN and Infinity are not measurements")
    return number


UtcTimestamp = Annotated[
    datetime,
    BeforeValidator(utc_timestamp),
    PlainSerializer(
        lambda value: value.isoformat().replace("+00:00", "Z"), return_type=str, when_used="json"
    ),
    Field(description="UTC ISO-8601 timestamp with explicit offset; JSON normalizes to Z."),
]
DecimalValue = Annotated[
    Decimal,
    BeforeValidator(finite_decimal),
    Field(allow_inf_nan=False),
    PlainSerializer(str, return_type=str, when_used="json"),
]
Energy = Annotated[
    DecimalValue,
    Field(
        ge=0,
        max_digits=20,
        decimal_places=6,
        description="Non-negative energy in kWh, DECIMAL(20,6).",
    ),
]
SignedEnergy = Annotated[
    DecimalValue,
    Field(
        decimal_places=6,
        description="Signed analytical energy in kWh with up to six fractional digits; aggregate totals may exceed a single DECIMAL(20,6) source row.",
    ),
]
Percent = Annotated[
    DecimalValue,
    Field(
        ge=0,
        le=100,
        description="Percentage or evidence index in [0,100]; never a probability of wrongdoing.",
    ),
]
SignedPercent = Annotated[
    DecimalValue,
    Field(
        description="Signed percentage; imbalance and deviations can be negative and are not bounded to [0,100]."
    ),
]
Fraction = Annotated[DecimalValue, Field(ge=0, le=1)]
PositiveId = Annotated[
    int,
    Field(
        strict=True,
        ge=1,
        description="Existing internal numeric database identifier, distinct from operational code.",
    ),
]
Count = Annotated[
    int,
    Field(
        strict=True, ge=0, description="Non-negative count for the stated population and period."
    ),
]
Code = Annotated[str, Field(strict=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
BoundedText = Annotated[str, Field(strict=True, min_length=1, max_length=255)]


class EntityType(StrEnum):
    METER = "METER"
    TRANSFORMER = "TRANSFORMER"


class Severity(StrEnum):
    NORMAL = "NORMAL"
    WATCH = "WATCH"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    WATCH = "WATCH"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class EnergySource(StrEnum):
    MEASURED = "MEASURED"
    DERIVED = "DERIVED"
    ESTIMATED = "ESTIMATED"
    UNAVAILABLE = "UNAVAILABLE"


class EnergyAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class CanonicalContract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)
    schema_version: Literal[1] = Field(
        default=1, description="Version of this canonical schema; unsupported versions fail closed."
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value


class PeriodContract(CanonicalContract):
    period_start: UtcTimestamp
    period_end: UtcTimestamp

    @model_validator(mode="after")
    def positive_period(self):
        if self.period_end <= self.period_start:
            raise ValueError("period_end must follow period_start")
        if self.period_end - self.period_start > timedelta(days=90):
            raise ValueError("read-model periods are limited to 90 days")
        return self


class IntervalContract(CanonicalContract):
    interval_start: UtcTimestamp
    interval_end: UtcTimestamp
    interval_minutes: Literal[15] = 15

    @model_validator(mode="after")
    def aligned_interval(self):
        if self.interval_end - self.interval_start != timedelta(minutes=15):
            raise ValueError("interval must span exactly 15 minutes")
        if (
            self.interval_start.minute % 15
            or self.interval_start.second
            or self.interval_start.microsecond
        ):
            raise ValueError("interval boundaries must align to UTC quarter-hours")
        return self


class SuccessResponse[T](CanonicalContract):
    data: T
    request_id: UUID


class PaginatedResponse[T](CanonicalContract):
    items: list[T] = Field(max_length=1000)
    page: Annotated[int, Field(strict=True, ge=1, le=10000)]
    page_size: Annotated[int, Field(strict=True, ge=1, le=1000)]
    total: Count
    pages: Count
    request_id: UUID

    @model_validator(mode="after")
    def consistent_page(self):
        expected_pages = (self.total + self.page_size - 1) // self.page_size
        if self.pages != expected_pages or len(self.items) > self.page_size:
            raise ValueError("pagination counts are inconsistent")
        if len(self.items) > self.total:
            raise ValueError("page contains more items than total")
        return self
