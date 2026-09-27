"""Measured, successfully derived and explicitly unavailable energy states."""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import (
    CanonicalContract,
    Count,
    DecimalValue,
    Energy,
    EnergyAvailability,
    EnergySource,
    IntervalContract,
    Percent,
    PositiveId,
    SignedEnergy,
    SignedPercent,
)


class RegisterStatus(StrEnum):
    NORMAL = "NORMAL"
    RESET = "RESET"
    ROLLOVER = "ROLLOVER"
    REPLACEMENT = "REPLACEMENT"
    INVALID = "INVALID"


class RegisterDeltaContract(CanonicalContract):
    previous_register_kwh: Energy
    current_register_kwh: Energy
    raw_delta_kwh: SignedEnergy
    meter_multiplier: Annotated[DecimalValue, Field(gt=0)]
    calculated_energy_kwh: Energy | None
    register_status: RegisterStatus
    recovery_policy: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def delta_semantics(self):
        if self.raw_delta_kwh != self.current_register_kwh - self.previous_register_kwh:
            raise ValueError("raw delta must retain the signed register difference")
        if self.register_status == RegisterStatus.NORMAL:
            if self.raw_delta_kwh < 0:
                raise ValueError("negative register delta cannot have NORMAL status")
            if self.calculated_energy_kwh != self.raw_delta_kwh * self.meter_multiplier:
                raise ValueError("normal interval energy must equal signed delta times multiplier")
        elif self.register_status == RegisterStatus.ROLLOVER:
            if not self.recovery_policy or self.calculated_energy_kwh is None:
                raise ValueError("recovered rollover requires a policy and a calculated value")
        elif self.calculated_energy_kwh is not None:
            raise ValueError("reset, replacement and invalid deltas cannot invent interval energy")
        return self


class OptionalEnergyContract(CanonicalContract):
    energy_kwh: SignedEnergy | None
    source: EnergySource
    unavailable_reason: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def available_value(self):
        if (self.energy_kwh is None) != (self.source == EnergySource.UNAVAILABLE):
            raise ValueError(
                "null energy requires UNAVAILABLE source and available energy requires a source"
            )
        return self


class EnergyMetricContract(CanonicalContract):
    value_kwh: SignedEnergy | None
    availability: EnergyAvailability
    source: EnergySource
    expected_intervals: Count
    valid_intervals: Count
    unavailable_reason: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def coverage(self):
        if self.valid_intervals > self.expected_intervals:
            raise ValueError("valid intervals cannot exceed expected intervals")
        if (self.value_kwh is None) != (self.availability == EnergyAvailability.UNAVAILABLE):
            raise ValueError("UNAVAILABLE means null; AVAILABLE/PARTIAL require a numeric value")
        if (self.value_kwh is None) != (self.source == EnergySource.UNAVAILABLE):
            raise ValueError("energy source must agree with availability")
        if self.availability == EnergyAvailability.AVAILABLE and (
            self.expected_intervals == 0 or self.valid_intervals != self.expected_intervals
        ):
            raise ValueError("AVAILABLE requires complete non-empty coverage")
        if self.availability == EnergyAvailability.PARTIAL and not (
            0 < self.valid_intervals < self.expected_intervals
        ):
            raise ValueError("PARTIAL requires some, but not all, expected intervals")
        return self


class MeterIntervalEnergyContract(IntervalContract):
    meter_id: PositiveId
    import_energy_kwh: Energy
    export_energy_kwh: Energy
    import_delta_kwh: Energy
    export_delta_kwh: Energy
    multiplier: Annotated[DecimalValue, Field(gt=0)]
    register_status: RegisterStatus
    quality: Literal["VALID", "ESTIMATED"]
    calculation_source: EnergySource = EnergySource.DERIVED
    calculation_policy: str | None = Field(default=None, max_length=255)
    avg_import_power_kw: Annotated[DecimalValue, Field(ge=0)] | None = None
    avg_export_power_kw: Annotated[DecimalValue, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def successful_derivation(self):
        if self.register_status in {
            RegisterStatus.INVALID,
            RegisterStatus.RESET,
            RegisterStatus.REPLACEMENT,
        }:
            raise ValueError("unrecoverable intervals must use UnavailableIntervalEnergyContract")
        if self.register_status == RegisterStatus.ROLLOVER and not self.calculation_policy:
            raise ValueError("rollover recovery requires an explicit policy")
        if self.quality == "ESTIMATED" and (
            self.calculation_source != EnergySource.ESTIMATED or not self.calculation_policy
        ):
            raise ValueError("estimates require ESTIMATED source and a documented method")
        if self.calculation_source == EnergySource.UNAVAILABLE:
            raise ValueError("successful interval energy cannot be unavailable")
        if self.import_energy_kwh != self.import_delta_kwh * self.multiplier:
            raise ValueError("import energy must equal register delta times multiplier")
        if self.export_energy_kwh != self.export_delta_kwh * self.multiplier:
            raise ValueError("export energy must equal register delta times multiplier")
        for energy, power in (
            (self.import_energy_kwh, self.avg_import_power_kw),
            (self.export_energy_kwh, self.avg_export_power_kw),
        ):
            if power is not None and power != energy * 4:
                raise ValueError("average power must equal interval energy divided by 0.25 hours")
        return self


class UnavailableIntervalEnergyContract(IntervalContract):
    entity_id: PositiveId
    entity_type: Literal["METER", "TRANSFORMER"]
    import_energy_kwh: None = None
    export_energy_kwh: None = None
    availability: Literal["UNAVAILABLE"] = "UNAVAILABLE"
    calculation_status: Literal[
        "MISSING_START",
        "MISSING_END",
        "MISSING_BOTH",
        "REGISTER_RESET",
        "METER_REPLACED",
        "INVALID",
        "OUT_OF_ORDER",
        "INVALID_MULTIPLIER",
        "SOURCE_CONFLICT",
    ]
    unavailable_reason: str = Field(min_length=1, max_length=500)


def validate_accounting(input_energy, downstream_energy, difference, percent):
    if input_energy is None or downstream_energy is None:
        if difference is not None or percent is not None:
            raise ValueError("missing accounting operands require null difference and percentage")
        return
    if difference is not None and abs(difference - (input_energy - downstream_energy)) > Decimal(
        "0.000001"
    ):
        raise ValueError("accounting difference must equal net input minus downstream")
    if input_energy <= 0 or difference is None:
        if percent is not None:
            raise ValueError("nonpositive input or missing difference requires null percentage")
    elif percent is not None and abs(percent - difference / input_energy * 100) > Decimal("0.0001"):
        raise ValueError("accounting percentage is inconsistent with its operands")


class AnalyticalEnergyContract(CanonicalContract):
    input_energy_kwh: SignedEnergy | None
    downstream_energy_kwh: SignedEnergy | None
    accounting_difference_kwh: SignedEnergy | None
    accounting_difference_percent: SignedPercent | None
    input_energy_source: EnergySource = EnergySource.UNAVAILABLE
    downstream_energy_source: EnergySource = EnergySource.UNAVAILABLE
    accounting_difference_source: EnergySource = EnergySource.UNAVAILABLE
    energy_availability: EnergyAvailability = EnergyAvailability.UNAVAILABLE
    energy_basis: Literal["NET_IMPORT"] = "NET_IMPORT"
    availability_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def analytical_semantics(self):
        for value, source in (
            (self.input_energy_kwh, self.input_energy_source),
            (self.downstream_energy_kwh, self.downstream_energy_source),
            (self.accounting_difference_kwh, self.accounting_difference_source),
        ):
            if (value is None) != (source == EnergySource.UNAVAILABLE):
                raise ValueError("each analytical energy value must agree with its source")
        values = (self.input_energy_kwh, self.downstream_energy_kwh)
        if self.energy_availability == EnergyAvailability.UNAVAILABLE and any(
            v is not None for v in values
        ):
            raise ValueError("numeric analytical energy cannot have UNAVAILABLE coverage")
        if self.energy_availability != EnergyAvailability.UNAVAILABLE and all(
            v is None for v in values
        ):
            raise ValueError("available or partial analytical energy requires a value")
        if self.energy_availability == EnergyAvailability.AVAILABLE and any(
            v is None for v in values
        ):
            raise ValueError("AVAILABLE accounting requires both operands")
        validate_accounting(
            *values, self.accounting_difference_kwh, self.accounting_difference_percent
        )
        return self


class TransformerEnergyBalanceContract(IntervalContract):
    transformer_id: PositiveId
    transformer_input_energy_kwh: SignedEnergy | None
    downstream_meter_energy_kwh: SignedEnergy | None
    accounting_difference_kwh: SignedEnergy | None
    accounting_difference_percent: SignedPercent | None
    expected_meter_count: Count
    valid_meter_count: Count
    data_completeness_percent: Percent | None
    confidence_score: Percent | None
    energy_availability: EnergyAvailability
    energy_basis: Literal["NET_IMPORT"] = "NET_IMPORT"
    availability_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def valid_balance(self):
        if self.valid_meter_count > self.expected_meter_count:
            raise ValueError("valid meter count exceeds expected meter count")
        if self.energy_availability == EnergyAvailability.AVAILABLE and (
            self.valid_meter_count != self.expected_meter_count
            or self.expected_meter_count == 0
            or self.transformer_input_energy_kwh is None
            or self.downstream_meter_energy_kwh is None
        ):
            raise ValueError("complete balance requires valid input and full non-empty population")
        if (
            self.valid_meter_count < self.expected_meter_count
            and self.accounting_difference_kwh is not None
        ):
            raise ValueError(
                "partial downstream population cannot produce a complete accounting difference"
            )
        validate_accounting(
            self.transformer_input_energy_kwh,
            self.downstream_meter_energy_kwh,
            self.accounting_difference_kwh,
            self.accounting_difference_percent,
        )
        return self
