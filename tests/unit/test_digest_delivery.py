"""``DeliverDigests`` orchestration, driven with fakes (docs/DESIGN.md 4.2, 6.5)."""

from __future__ import annotations

import datetime as dt
import uuid

from tc_application.digest_delivery import DeliverDigests
from tc_domain.digest import DigestContent
from tc_domain.digest_ports import PendingDigest
from tests.unit.fakes import FakeDigestOutbox, FakeDigestSender, FakeDigestSource

CONTENT = DigestContent(
    title="Daily digest",
    body_markdown="## Summary\n\nsomething",
    window_start=dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC),
    window_end=dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC),
)


def a_pending(*, attempts: int = 1) -> PendingDigest:
    return PendingDigest(
        event_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        attempts=attempts,
    )


async def test_no_pending_events_delivers_nothing() -> None:
    outbox = FakeDigestOutbox()
    deliver = DeliverDigests(
        outbox=outbox, source=FakeDigestSource(CONTENT), sender=FakeDigestSender()
    )

    delivered = await deliver()

    assert delivered == 0
    assert outbox.delivered == []
    assert outbox.failed == []


async def test_a_successful_send_marks_the_event_delivered() -> None:
    event = a_pending()
    outbox = FakeDigestOutbox([event])
    sender = FakeDigestSender(succeed=True)
    source = FakeDigestSource(CONTENT)
    deliver = DeliverDigests(outbox=outbox, source=source, sender=sender)

    delivered = await deliver()

    assert delivered == 1
    assert outbox.delivered == [event.event_id]
    assert outbox.failed == []
    assert len(sender.sent) == 1
    # The event's own workspace and run travel into the read, not just the
    # bare document/revision ids - see PendingDigest's docstring.
    assert source.calls == [
        (event.workspace_id, event.run_id, event.document_id, event.revision_id)
    ]


async def test_a_sender_returning_false_marks_the_event_failed_not_delivered() -> None:
    event = a_pending(attempts=2)
    outbox = FakeDigestOutbox([event])
    sender = FakeDigestSender(succeed=False)
    deliver = DeliverDigests(outbox=outbox, source=FakeDigestSource(CONTENT), sender=sender)

    delivered = await deliver()

    assert delivered == 0
    assert outbox.delivered == []
    assert outbox.failed == [
        {"event_id": event.event_id, "attempts": 2, "error": "send_returned_false"}
    ]


async def test_a_source_lookup_failure_marks_the_event_failed_with_a_sanitized_error() -> None:
    event = a_pending(attempts=1)
    outbox = FakeDigestOutbox([event])
    source = FakeDigestSource(missing={event.revision_id})
    deliver = DeliverDigests(outbox=outbox, source=source, sender=FakeDigestSender())

    delivered = await deliver()

    assert delivered == 0
    assert outbox.delivered == []
    assert len(outbox.failed) == 1
    assert outbox.failed[0]["error"] == "DigestNotFound"


async def test_a_sender_exception_marks_the_event_failed_with_a_sanitized_error() -> None:
    event = a_pending()
    outbox = FakeDigestOutbox([event])
    sender = FakeDigestSender(raises=RuntimeError("connection reset: token=abc123"))
    deliver = DeliverDigests(outbox=outbox, source=FakeDigestSource(CONTENT), sender=sender)

    delivered = await deliver()

    assert delivered == 0
    assert outbox.failed[0]["error"] == "RuntimeError"


async def test_multiple_events_are_each_delivered_independently() -> None:
    good = a_pending()
    bad = a_pending()
    outbox = FakeDigestOutbox([good, bad])
    source = FakeDigestSource(CONTENT, missing={bad.revision_id})
    sender = FakeDigestSender()
    deliver = DeliverDigests(outbox=outbox, source=source, sender=sender)

    delivered = await deliver()

    assert delivered == 1
    assert outbox.delivered == [good.event_id]
    assert len(outbox.failed) == 1
    assert outbox.failed[0]["event_id"] == bad.event_id
