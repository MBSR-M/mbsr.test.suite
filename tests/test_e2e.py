import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from opengrid.config import settings

pytestmark = pytest.mark.e2e


def test_real_stream_to_case_and_workflow():
    suffix = uuid4().hex[:8]
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)
    with (
        httpx.Client(
            base_url="http://api:8000", headers={"X-API-Key": settings().api_key}, timeout=30
        ) as api,
        httpx.Client(
            base_url="http://ingestion:8000", headers={"X-API-Key": settings().api_key}, timeout=30
        ) as ingest,
    ):
        r = api.post(
            "/api/v1/transformers",
            json={"code": f"E2E-T-{suffix}", "valid_from": start.isoformat()},
        )
        r.raise_for_status()
        transformer = r.json()
        r = api.post(
            "/api/v1/meters",
            json={
                "code": f"E2E-M-{suffix}",
                "transformer_id": transformer["id"],
                "valid_from": start.isoformat(),
            },
        )
        r.raise_for_status()
        meter = r.json()
        batch = []
        # 28 historical samples in every persistence slot, followed by six qualifying slots.
        for day in range(29):
            for slot in range(6):
                end = start + timedelta(days=day, minutes=15 * (slot + 1))
                for asset, value in ((transformer, "100"), (meter, "80" if day == 28 else "93")):
                    batch.append(
                        {
                            "event_id": str(uuid4()),
                            "entity_code": asset["code"],
                            "measurement_kind": "INTERVAL_ENERGY",
                            "energy_basis": "PRIMARY",
                            "reading_time": end.isoformat(),
                            "interval_start": (end - timedelta(minutes=15)).isoformat(),
                            "import_energy_kwh": value,
                            "received_at": end.isoformat(),
                        }
                    )
        r = ingest.post("/api/v1/readings/bulk", json=batch)
        r.raise_for_status()
        duplicate = ingest.post("/api/v1/readings/bulk", json=batch)
        assert duplicate.status_code == 202
        deadline = time.monotonic() + 240
        case = None
        while time.monotonic() < deadline:
            cases = api.get("/api/v1/investigations", params={"limit": 1000}).json()
            case = next((c for c in cases if c["asset_id"] == transformer["id"]), None)
            if case:
                break
            time.sleep(2)
        assert case is not None, api.get("/api/v1/system").text
        assert float(case["evidence"]["accounting_difference_percent"]) == pytest.approx(20)
        assert float(case["evidence"]["baseline"]["median"]) == pytest.approx(7)
        assert case["evidence"]["persistence_slots"] >= 4
        response = api.patch(
            f"/api/v1/investigations/{case['id']}",
            json={"version": case["version"], "status": "ASSIGNED", "assigned_to": "test-operator"},
        )
        assert response.status_code == 200
        stale = api.patch(
            f"/api/v1/investigations/{case['id']}",
            json={"version": case["version"], "status": "DISMISSED", "resolution": "stale update"},
        )
        assert stale.status_code == 409
        history = api.get(f"/api/v1/investigations/{case['id']}/history").json()
        assert len(history) == 2
