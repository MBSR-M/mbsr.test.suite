from datetime import UTC, datetime
from decimal import Decimal as D
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from opengrid.contracts import ReadingInput
from opengrid.db import Asset, Configuration, Interval, Outbox, Reading, session_factory
from opengrid.services import Conflict, accept_reading, normalize

pytestmark = pytest.mark.integration


def test_mysql_duplicate_and_late_boundary():
    factory = session_factory()
    code = "IT-" + uuid4().hex[:16]
    time = datetime(2026, 1, 1, tzinfo=UTC)
    with factory.begin() as s:
        asset = Asset(code=code, kind="TRANSFORMER")
        s.add(asset)
        s.flush()
        asset_id = asset.id
        s.add(
            Configuration(
                asset_id=asset.id,
                valid_from=time.replace(tzinfo=None),
                multiplier=10,
                import_only=True,
            )
        )

    def reading(minute, value):
        return ReadingInput(
            event_id=uuid4(),
            entity_code=code,
            measurement_kind="CUMULATIVE_REGISTER",
            reading_time=time.replace(minute=minute),
            import_energy_total_kwh=D(value),
        )

    left, right, middle = reading(0, "100"), reading(30, "103"), reading(15, "101")
    with factory.begin() as s:
        accept_reading(s, left)
        accept_reading(s, right)
        accept_reading(s, left)
        assert (
            s.scalar(select(func.count()).select_from(Reading).where(Reading.asset_id == asset_id))
            == 2
        )
        normalize(s, asset_id, time.replace(tzinfo=None))
    with factory.begin() as s:
        interval = s.scalar(select(Interval).where(Interval.asset_id == asset_id))
        assert interval.import_kwh is None
        accept_reading(s, middle)
        normalize(s, asset_id, time.replace(tzinfo=None))
        normalize(s, asset_id, time.replace(minute=15, tzinfo=None))
    with factory.begin() as s:
        rows = s.scalars(
            select(Interval).where(Interval.asset_id == asset_id).order_by(Interval.start)
        ).all()
        assert [r.import_kwh for r in rows] == [D(10), D(20)]
        with pytest.raises(Conflict):
            accept_reading(s, left.model_copy(update={"import_energy_total_kwh": D(200)}))
        assert s.scalar(select(func.count()).select_from(Outbox).where(Outbox.key == code)) == 3


def test_independent_meter_anomaly_and_correction():
    from datetime import timedelta

    from opengrid.db import Anomaly
    from opengrid.services import meter_anomaly_handler

    factory = session_factory()
    start = datetime(2026, 2, 1)
    with factory.begin() as s:
        asset = Asset(code="IT-M-" + uuid4().hex[:12], kind="METER")
        s.add(asset)
        s.flush()
        for day in range(8):
            s.add(
                Interval(
                    asset_id=asset.id,
                    start=start + timedelta(days=day),
                    import_kwh=D("0.1") if day == 7 else D(1),
                    export_kwh=0,
                    status="VALID",
                    evidence={},
                )
            )
        s.flush()
        end = start + timedelta(days=7)
        meter_anomaly_handler(s, {"asset_id": asset.id, "start": end.isoformat()})
        finding = s.scalar(select(Anomaly).where(Anomaly.asset_id == asset.id))
        assert finding.anomaly_type == "CONSUMPTION_DROP"
        row = s.scalar(select(Interval).where(Interval.asset_id == asset.id, Interval.start == end))
        row.import_kwh = D(1)
        row.revision += 1
        s.flush()
        meter_anomaly_handler(s, {"asset_id": asset.id, "start": end.isoformat(), "revision": 2})
        assert finding.status == "SUPERSEDED"
