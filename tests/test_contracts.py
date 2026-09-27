from uuid import uuid4

import pytest
from pydantic import ValidationError

from opengrid.contracts import ReadingInput


def payload():
    return {
        "event_id": str(uuid4()),
        "entity_code": "M-1",
        "measurement_kind": "CUMULATIVE_REGISTER",
        "reading_time": "2026-01-01T10:15:00Z",
        "import_energy_total_kwh": "123.45",
    }


def test_explicit_energy_and_time():
    assert ReadingInput(**payload()).import_energy_total_kwh.as_tuple().exponent == -2
    for changes in (
        {"reading_time": "2026-01-01T10:15:00"},
        {"reading_time": "2026-01-01T10:17:00Z"},
        {"energy_import_kwh": 2},
        {"import_energy_kwh": 2},
        {"import_energy_total_kwh": "NaN"},
    ):
        with pytest.raises(ValidationError):
            ReadingInput(**(payload() | changes))


def test_interval_contract():
    body = payload()
    body.pop("import_energy_total_kwh")
    body.update(
        measurement_kind="INTERVAL_ENERGY",
        import_energy_kwh="0.632",
        interval_start="2026-01-01T10:00:00Z",
    )
    assert ReadingInput(**body).import_energy_kwh is not None
