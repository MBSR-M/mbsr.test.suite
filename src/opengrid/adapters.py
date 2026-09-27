"""Bounded CSV and external-Kafka adapters into the canonical ingestion API."""

import argparse
import csv
import json
from uuid import NAMESPACE_URL, uuid5

import httpx
from confluent_kafka import Consumer

from opengrid.config import settings
from opengrid.contracts import ReadingInput


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://ingestion:8000")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv")
    source.add_argument("--kafka-topic")
    parser.add_argument("--group", default="opengrid-external-ingestion-v1")
    args = parser.parse_args()
    with httpx.Client(
        base_url=args.url, headers={"X-API-Key": settings().api_key}, timeout=60
    ) as client:

        def submit(rows):
            response = client.post("/api/v1/readings/bulk", json=rows)
            response.raise_for_status()

        if args.csv:
            batch = []
            with open(args.csv, newline="", encoding="utf-8-sig") as handle:
                for row in csv.DictReader(handle):
                    row = {k: v for k, v in row.items() if v != ""}
                    row.setdefault(
                        "event_id", str(uuid5(NAMESPACE_URL, json.dumps(row, sort_keys=True)))
                    )
                    batch.append(ReadingInput.model_validate(row).model_dump(mode="json"))
                    if len(batch) == 500:
                        submit(batch)
                        batch.clear()
                if batch:
                    submit(batch)
        else:
            if args.kafka_topic.startswith("openami."):
                raise ValueError("external adapter must not consume internal OpenGrid topics")
            consumer = Consumer(
                {
                    "bootstrap.servers": settings().kafka_bootstrap_servers,
                    "group.id": args.group,
                    "enable.auto.commit": False,
                    "auto.offset.reset": "earliest",
                }
            )
            consumer.subscribe([args.kafka_topic])
            try:
                while True:
                    message = consumer.poll(1)
                    if message is None:
                        continue
                    if message.error():
                        raise RuntimeError(str(message.error()))
                    envelope = json.loads(message.value())
                    row = ReadingInput.model_validate(
                        envelope["payload"]
                        | {
                            "event_id": envelope["event_id"],
                            "schema_version": envelope["schema_version"],
                        }
                    )
                    submit([row.model_dump(mode="json")])
                    consumer.commit(message=message, asynchronous=False)
            finally:
                consumer.close()


if __name__ == "__main__":
    main()
