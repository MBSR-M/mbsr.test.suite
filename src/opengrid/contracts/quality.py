"""Quality states preserve missing, late, invalid and recovered observations."""

from enum import StrEnum
from typing import Annotated

from pydantic import Field, model_validator

from .common import Count, EntityType, IntervalContract, PositiveId


class ReadingQuality(StrEnum):
    VALID = "VALID"
    MISSING = "MISSING"
    DUPLICATE = "DUPLICATE"
    LATE = "LATE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    INVALID = "INVALID"
    REGISTER_RESET = "REGISTER_RESET"
    REGISTER_ROLLOVER = "REGISTER_ROLLOVER"
    METER_REPLACED = "METER_REPLACED"
    ESTIMATED = "ESTIMATED"


class DataQualityContract(IntervalContract):
    entity_id: PositiveId
    entity_type: EntityType
    expected: Annotated[bool, Field(strict=True)]
    received: Annotated[bool, Field(strict=True)]
    quality: ReadingQuality
    delay_seconds: Count | None
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def observed_quality(self):
        if self.quality == ReadingQuality.MISSING and self.received:
            raise ValueError("a missing reading cannot be received")
        if not self.received and self.delay_seconds is not None:
            raise ValueError("unreceived readings do not have a measured arrival delay")
        return self
