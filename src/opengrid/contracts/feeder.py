"""Feeder topology and analytics remain separate contracts."""

from typing import Annotated

from pydantic import Field

from .common import (
    CanonicalContract,
    Code,
    Count,
    HealthStatus,
    Percent,
    PeriodContract,
    PositiveId,
    UtcTimestamp,
)
from .energy import AnalyticalEnergyContract


class FeederContract(CanonicalContract):
    feeder_id: PositiveId
    feeder_code: Code
    name: str | None = Field(default=None, max_length=255)
    substation: str | None = Field(default=None, max_length=255)
    active: Annotated[bool, Field(strict=True)] = True
    transformer_count: Count
    meter_count: Count
    created_at: UtcTimestamp | None = None
    updated_at: UtcTimestamp | None = None
    metadata_unavailable_reason: str | None = Field(default=None, max_length=255)


class FeederSummaryContract(AnalyticalEnergyContract, PeriodContract):
    feeder_id: PositiveId
    feeder_code: Code
    name: str | None
    substation: str | None
    transformer_count: Count
    meter_count: Count
    data_completeness_percent: Percent | None
    anomaly_count: Count
    high_anomaly_count: Count
    critical_anomaly_count: Count
    open_investigation_count: Count
    confidence_score: Percent | None
    status: HealthStatus


class FeederTrendPointContract(AnalyticalEnergyContract):
    timestamp: UtcTimestamp
    data_completeness_percent: Percent | None
    anomaly_count: Count


class FeederTrendContract(PeriodContract):
    feeder_id: PositiveId
    interval_minutes: Annotated[int, Field(strict=True, ge=15, le=1440)]
    points: list[FeederTrendPointContract] = Field(max_length=3000)


class MeterTopologyContract(CanonicalContract):
    meter_id: PositiveId
    meter_no: Code
    consumer_type: str | None = Field(default=None, max_length=64)
    status: HealthStatus


class TransformerTopologyContract(CanonicalContract):
    transformer_id: PositiveId
    transformer_code: Code
    name: str | None
    meter_count: Count
    status: HealthStatus
    meters: list[MeterTopologyContract] = Field(max_length=1000)
    meters_truncated: bool = False


class FeederTopologyContract(CanonicalContract):
    feeder: FeederContract
    transformers: list[TransformerTopologyContract] = Field(max_length=100)
    page: Annotated[int, Field(strict=True, ge=1)] = 1
    page_size: Annotated[int, Field(strict=True, ge=1, le=100)] = 20
    total: Count
    pages: Count
