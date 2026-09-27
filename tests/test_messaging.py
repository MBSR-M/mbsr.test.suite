from uuid import uuid4

import pytest
from sqlalchemy import select

from opengrid.db import Inbox, Job, session_factory
from opengrid.messaging import rabbit
from opengrid.services import emit
from opengrid.worker import process

pytestmark = pytest.mark.integration


def test_rabbit_confirm_and_manual_ack():
    connection, channel = rabbit()
    try:
        queue = "opengrid-test-" + uuid4().hex
        channel.queue_declare(queue=queue, exclusive=True, auto_delete=True)
        channel.basic_publish("", queue, b"test", mandatory=True)
        method, _, body = channel.basic_get(queue, auto_ack=False)
        assert body == b"test"
        channel.basic_nack(method.delivery_tag, requeue=True)
        method, _, body = channel.basic_get(queue, auto_ack=False)
        assert method.redelivered and body == b"test"
        channel.basic_ack(method.delivery_tag)
    finally:
        connection.close()


def test_inbox_rolls_back_with_failed_domain_effect():
    factory = session_factory()
    event_id = str(uuid4())
    envelope = {"event_id": event_id, "schema_version": 1, "payload": {"reading_id": -1}}
    with pytest.raises(AttributeError):
        process(factory, "quality", envelope, "openami.meter.reading")
    with factory.begin() as s:
        assert s.get(Inbox, ("quality", event_id)) is None


def test_job_and_outbox_rollback_together():
    factory = session_factory()
    job_id = str(uuid4())
    with pytest.raises(RuntimeError), factory.begin() as s:
        s.add(Job(id=job_id, payload={}))
        emit(s, "api", "rabbit:openami.reprocessing", job_id, {"job_id": job_id})
        s.flush()
        raise RuntimeError("simulated process failure")
    with factory.begin() as s:
        assert s.scalar(select(Job).where(Job.id == job_id)) is None
