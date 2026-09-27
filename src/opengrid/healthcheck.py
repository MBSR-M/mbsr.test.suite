"""Worker heartbeat check for Compose and operational smoke checks."""

from datetime import timedelta

from opengrid.config import settings
from opengrid.db import Heartbeat, now, session_factory


def main():
    with session_factory()() as s:
        row = s.get(Heartbeat, settings().service)
        if row is None or now() - row.time > timedelta(seconds=120):
            raise SystemExit("worker heartbeat is stale")


if __name__ == "__main__":
    main()
