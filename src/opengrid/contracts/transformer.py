"""Transformer topology metadata and measured cumulative registers."""

from typing import Annotated

from pydantic import Field

from .common import CanonicalContract, Code, Count, DecimalValue, PositiveId
from .meter import RegisterReadingContract


class TransformerContract(CanonicalContract):
    transformer_id: PositiveId
    transformer_code: Code
    feeder_id: PositiveId | None
    name: str | None = Field(default=None, max_length=255)
    capacity_kva: Annotated[DecimalValue, Field(gt=0)] | None = None
    latitude: Annotated[DecimalValue, Field(ge=-90, le=90)] | None = None
    longitude: Annotated[DecimalValue, Field(ge=-180, le=180)] | None = None
    meter_count: Count
    active: Annotated[bool, Field(strict=True)] = True


class TransformerReadingContract(RegisterReadingContract):
    transformer_id: PositiveId
    transformer_code: Code | None = None
