"""Pure, deterministic accounting. No transport or persistence imports."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from statistics import median

D = Decimal
QUANTUM = D("0.000001")
STEP = timedelta(minutes=15)


def energy(value: Decimal) -> Decimal:
    return value.quantize(QUANTUM, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class Normalized:
    value: Decimal | None
    status: str


def register_delta(
    previous: Decimal,
    current: Decimal,
    multiplier: Decimal = D(1),
    *,
    same_epoch: bool = True,
    modulus: Decimal | None = None,
    rollover_confirmed: bool = False,
    max_energy: Decimal | None = None,
) -> Normalized:
    if (
        not all(v.is_finite() and v >= 0 for v in (previous, current, multiplier))
        or multiplier == 0
    ):
        return Normalized(None, "INVALID")
    if not same_epoch:
        return Normalized(None, "METER_REPLACED")
    delta = current - previous
    status = "VALID"
    if delta < 0:
        if not (modulus and rollover_confirmed and max_energy is not None):
            return Normalized(None, "REGISTER_RESET")
        if not (previous < modulus and current < modulus):
            return Normalized(None, "INVALID")
        delta = modulus - previous + current
        status = "REGISTER_ROLLOVER"
    result = energy(delta * multiplier)
    if max_energy is not None and result > max_energy:
        return Normalized(None, "INVALID")
    return Normalized(result, status)


@dataclass(frozen=True)
class BalanceResult:
    difference: Decimal | None
    percent: Decimal | None
    completeness: Decimal | None
    status: str


def calculate_energy_balance(
    transformer: Decimal | None, downstream: Decimal, expected: int, valid: int
) -> BalanceResult:
    if not 0 <= valid <= expected:
        raise ValueError("invalid population counts")
    if expected == 0:
        return BalanceResult(None, None, None, "EMPTY_TOPOLOGY")
    completeness = D(valid) * 100 / D(expected)
    if transformer is None:
        return BalanceResult(None, None, completeness, "MISSING_TRANSFORMER")
    difference = energy(transformer - downstream)
    if valid != expected:
        return BalanceResult(difference, None, completeness, "INCOMPLETE")
    if transformer <= D("0.001"):
        return BalanceResult(difference, None, completeness, "NON_POSITIVE_NET_INPUT")
    return BalanceResult(difference, difference / transformer * 100, completeness, "COMPLETE")


def calculate_baseline(values: list[Decimal], minimum: int) -> dict[str, Decimal] | None:
    if len(values) < minimum:
        return None
    center = D(median(values))
    mean = sum(values, D(0)) / D(len(values))
    variance = sum(((x - mean) ** 2 for x in values), D(0)) / D(len(values))
    return {
        "median": center,
        "mean": mean,
        "stddev": variance.sqrt(),
        "mad": D(median([abs(x - center) for x in values])),
    }


def detect_persistent_anomaly(
    slots: list[tuple[datetime, bool, bool]], required: int = 4, window: int = 6
) -> bool:
    if len(slots) != window or not slots[-1][2] or not all(s[1] for s in slots):
        return False
    if any(b[0] - a[0] != STEP for a, b in zip(slots, slots[1:], strict=False)):
        return False
    return sum(s[2] for s in slots) >= required


def calculate_anomaly_score(components: tuple[Decimal, ...]) -> Decimal:
    if len(components) != 5 or any(not x.is_finite() or not 0 <= x <= 1 for x in components):
        raise ValueError("five components in [0, 1] required")
    return sum((w * x for w, x in zip((35, 25, 20, 10, 10), components, strict=True)), D(0))


def consumption_deviation(current: Decimal, history: list[Decimal]) -> str | None:
    if len(history) < 7:
        return None
    baseline = D(median(history))
    if baseline < D("0.01"):
        return None
    if current < baseline * D("0.30"):
        return "CONSUMPTION_DROP"
    if current > baseline * D("2.50"):
        return "CONSUMPTION_SPIKE"
    return None
