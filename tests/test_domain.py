from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from opengrid.domain import (
    STEP,
    calculate_anomaly_score,
    calculate_baseline,
    calculate_energy_balance,
    consumption_deviation,
    detect_persistent_anomaly,
    register_delta,
)


def test_register_scaling():
    result = register_delta(D("12543.210"), D("12543.842"), D(10))
    assert result.value == D("6.320000")
    assert result.value * 4 == D("25.28")


def test_reset_not_assumed_rollover():
    assert register_delta(D("999.9"), D("0.1"), modulus=D(1000)).value is None
    assert register_delta(
        D("999.9"), D("0.1"), modulus=D(1000), rollover_confirmed=True, max_energy=D(1)
    ).value == D("0.2")
    assert register_delta(D(10), D(20), same_epoch=False).status == "METER_REPLACED"


@pytest.mark.parametrize("previous,current", [("-1", "2"), ("NaN", "2"), ("1", "Infinity")])
def test_invalid_register(previous, current):
    assert register_delta(D(previous), D(current)).value is None


def test_incomplete_not_a_loss_percentage():
    partial = calculate_energy_balance(D(100), D(0), 100, 0)
    assert partial.percent is None
    assert partial.completeness == 0
    assert partial.status == "INCOMPLETE"
    assert calculate_energy_balance(D(100), D(0), 0, 0).completeness is None


@pytest.mark.parametrize("value", ["0", "-1", "0.0001"])
def test_zero_and_reverse_input(value):
    assert calculate_energy_balance(D(value), D(0), 1, 1).percent is None


def test_balance_sign_and_baseline():
    assert calculate_energy_balance(D(100), D(92), 10, 10).percent == D(8)
    assert calculate_energy_balance(D(100), D(105), 10, 10).percent == D(-5)
    assert calculate_baseline([D(7), D(7), D(99)], 3)["median"] == 7
    assert calculate_baseline([D(7)], 14) is None


def test_persistence_requires_adjacent_eligible_slots():
    start = datetime(2026, 1, 1)
    slots = [(start + i * STEP, True, i >= 2) for i in range(6)]
    assert detect_persistent_anomaly(slots)
    assert not detect_persistent_anomaly(slots[:-1])
    slots[0] = (start - timedelta(days=1), True, True)
    assert not detect_persistent_anomaly(slots)
    slots[0] = (start, False, True)
    assert not detect_persistent_anomaly(slots)


def test_score_bounds_and_meter_detection():
    assert calculate_anomaly_score((D(1),) * 5) == 100
    with pytest.raises(ValueError):
        calculate_anomaly_score((D(2),) * 5)
    assert consumption_deviation(D("0.2"), [D(1)] * 7) == "CONSUMPTION_DROP"
    assert consumption_deviation(D(3), [D(1)] * 7) == "CONSUMPTION_SPIKE"
