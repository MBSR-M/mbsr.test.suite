import argparse
import json
import math
import random
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import httpx

from opengrid.config import settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        choices=[
            "normal",
            "missing-data",
            "communication-outage",
            "consumption-drop",
            "transformer-imbalance",
            "combined",
        ],
        default="transformer-imbalance",
    )
    parser.add_argument("--profile", choices=["quick", "full"], default="quick")
    parser.add_argument("--transformers", type=int)
    parser.add_argument("--meters-per-transformer", type=int)
    parser.add_argument("--days", type=int)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    full = args.profile == "full"
    transformers = args.transformers or (100 if full else 1)
    meters = args.meters_per_transformer or (100 if full else 5)
    days = args.days or 30
    if full and args.days is None:
        days = 90
    rng = random.Random(args.seed)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days)
    anomaly_start = today - timedelta(hours=6)
    headers = {"X-API-Key": settings().api_key}
    registers = {}
    with (
        httpx.Client(base_url="http://api:8000", headers=headers, timeout=60) as api,
        httpx.Client(base_url="http://ingestion:8000", headers=headers, timeout=60) as ingestion,
    ):

        def create(path, payload):
            response = api.post(path, json=payload)
            if response.status_code == 409:
                listing = api.get(path, params={"page_size": 1000}).json()
                return next(r["id"] for r in listing if r["code"] == payload["code"])
            response.raise_for_status()
            return response.json()["id"]

        groups = []
        for n in range(transformers):
            code = f"DT-{1047 + n}"
            transformer_id = create(
                "/api/v1/transformers", {"code": code, "valid_from": start.isoformat()}
            )
            members = []
            for m in range(meters):
                meter = f"M-{n:04d}-{m:04d}"
                create(
                    "/api/v1/meters",
                    {
                        "code": meter,
                        "valid_from": start.isoformat(),
                        "transformer_id": transformer_id,
                    },
                )
                members.append(meter)
                registers[meter] = 1000.0
            registers[code] = 10000.0
            groups.append((code, members))

        def reading(code, time):
            return {
                "event_id": str(
                    uuid5(NAMESPACE_URL, f"{args.seed}/{args.scenario}/{code}/{time.isoformat()}")
                ),
                "entity_code": code,
                "measurement_kind": "CUMULATIVE_REGISTER",
                "reading_time": time.isoformat(),
                "import_energy_total_kwh": f"{registers[code]:.6f}",
                "source": "SIMULATOR",
                "received_at": time.isoformat(),
            }

        batch = []
        # Quick profile uses six comparable slots per historical day, then six hours of continuous data.
        # Full profile includes every interval; quick intentionally has missing gaps between training windows.
        for index in range(days * 96 + 1):
            time = start + timedelta(minutes=15 * index)
            for code, members in groups:
                true_sum = 0.0
                for m, meter in enumerate(members):
                    amount = max(
                        0.03,
                        0.22 + 0.1 * math.sin(time.hour / 24 * math.tau) + rng.uniform(-0.02, 0.02),
                    )
                    true_sum += amount
                    changed = time >= anomaly_start and code == "DT-1047"
                    if changed and args.scenario in {"consumption-drop", "combined"} and m < 2:
                        amount *= 0.1
                    if index:
                        registers[meter] += amount
                    missing = changed and args.scenario == "communication-outage"
                    missing |= args.scenario in {"missing-data", "combined"} and rng.random() < 0.01
                    if not missing and (full or time.hour >= 18):
                        batch.append(reading(meter, time))
                imbalance = (
                    0.187
                    if time >= anomaly_start
                    and code == "DT-1047"
                    and args.scenario in {"transformer-imbalance", "combined"}
                    else 0.072
                )
                if index:
                    registers[code] += true_sum / (1 - imbalance)
                if full or time.hour >= 18:
                    batch.append(reading(code, time))
                if len(batch) >= 500:
                    response = ingestion.post("/api/v1/readings/bulk", json=batch)
                    response.raise_for_status()
                    batch.clear()
            if index % 96 == 0:
                print(json.dumps({"day": index // 96, "total_days": days}), flush=True)
        if batch:
            response = ingestion.post("/api/v1/readings/bulk", json=batch)
            response.raise_for_status()
    print(
        json.dumps(
            {
                "ground_truth": {
                    "scenario": args.scenario,
                    "transformer": "DT-1047",
                    "anomaly_start": anomaly_start.isoformat(),
                    "seed": args.seed,
                }
            }
        )
    )


if __name__ == "__main__":
    main()
