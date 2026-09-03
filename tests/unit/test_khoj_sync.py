"""``DeliverKhojSync``/``ForceKhojSync`` orchestration, driven with fakes
(docs/DESIGN.md 6.5, 7.2 step 11, 10) - same test shapes as
``tests/unit/test_digest_delivery.py``."""

from __future__ import annotations

import uuid

from tc_application.khoj_sync import DeliverKhojSync, ForceKhojSync
from tc_domain.khoj_export import DocumentExport
from tc_domain.khoj_sync_ports import PendingKhojSync
from tests.unit.fakes import (
    FakeKhojExportSource,
    FakeKhojForceSync,
    FakeKhojIndexRecorder,
    FakeKhojPort,
    FakeKhojSyncOutbox,
)

EXPORT = DocumentExport(
    document_id=uuid.uuid4(),
    revision_id=uuid.uuid4(),
    workspace_id=uuid.uuid4(),
    kind="project",
    stable_key="project:aurora",
    title="Aurora",
    body_markdown="## Summary\n\nsomething",
    window_start=None,
    window_end=None,
    entities=(),
    source_thought_ids=(1,),
)


def a_pending(*, attempts: int = 1, document_id: uuid.UUID | None = None) -> PendingKhojSync:
    return PendingKhojSync(
        event_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        document_id=document_id or uuid.uuid4(),
        attempts=attempts,
    )


def _deliver(
    outbox: FakeKhojSyncOutbox,
    *,
    source: FakeKhojExportSource | None = None,
    khoj: FakeKhojPort | None = None,
    recorder: FakeKhojIndexRecorder | None = None,
) -> DeliverKhojSync:
    return DeliverKhojSync(
        outbox=outbox,
        source=source or FakeKhojExportSource(EXPORT),
        khoj=khoj or FakeKhojPort(),
        recorder=recorder or FakeKhojIndexRecorder(),
    )


async def test_no_pending_events_syncs_nothing() -> None:
    outbox = FakeKhojSyncOutbox()
    deliver = _deliver(outbox)

    synced = await deliver()

    assert synced == 0
    assert outbox.delivered == []
    assert outbox.failed == []


async def test_a_successful_sync_marks_the_event_delivered_and_records_it() -> None:
    event = a_pending(document_id=EXPORT.document_id)
    outbox = FakeKhojSyncOutbox([event])
    khoj = FakeKhojPort()
    recorder = FakeKhojIndexRecorder()
    deliver = _deliver(outbox, khoj=khoj, recorder=recorder)

    synced = await deliver()

    assert synced == 1
    assert outbox.delivered == [event.event_id]
    assert outbox.failed == []
    assert len(khoj.indexed) == 1
    (files,) = khoj.indexed
    assert files[0].filename == EXPORT.filename
    assert recorder.recorded == [
        {
            "workspace_id": event.workspace_id,
            "document_id": EXPORT.document_id,
            "filename": EXPORT.filename,
            "revision_id": EXPORT.revision_id,
            "body_sha256": recorder.recorded[0]["body_sha256"],
        }
    ]
    assert isinstance(recorder.recorded[0]["body_sha256"], str)
    assert len(recorder.recorded[0]["body_sha256"]) == 64


async def test_an_export_lookup_failure_marks_the_event_failed_with_a_sanitized_error() -> None:
    event = a_pending()
    outbox = FakeKhojSyncOutbox([event])
    source = FakeKhojExportSource(EXPORT, missing={event.document_id})
    deliver = _deliver(outbox, source=source)

    synced = await deliver()

    assert synced == 0
    assert outbox.delivered == []
    assert len(outbox.failed) == 1
    assert outbox.failed[0]["error"] == "KhojExportNotFound"


async def test_a_khoj_unavailable_error_marks_the_event_failed_and_records_nothing() -> None:
    event = a_pending(document_id=EXPORT.document_id, attempts=2)
    outbox = FakeKhojSyncOutbox([event])
    khoj = FakeKhojPort(raises=RuntimeError("connection reset: token=abc123"))
    recorder = FakeKhojIndexRecorder()
    deliver = _deliver(outbox, khoj=khoj, recorder=recorder)

    synced = await deliver()

    assert synced == 0
    assert outbox.failed == [{"event_id": event.event_id, "attempts": 2, "error": "RuntimeError"}]
    assert recorder.recorded == []


async def test_a_recorder_failure_after_a_successful_index_still_fails_the_event() -> None:
    """Khoj already has the content by this point (index() succeeded) - the
    event must still be marked failed and retried, not silently dropped,
    since a retry is safe: HttpKhojClient.index() is idempotent by filename
    (tests/contract/khoj/test_khoj_client.py)."""
    event = a_pending(document_id=EXPORT.document_id, attempts=1)
    outbox = FakeKhojSyncOutbox([event])
    khoj = FakeKhojPort()
    recorder = FakeKhojIndexRecorder(raises=RuntimeError("db statement: ..."))
    deliver = _deliver(outbox, khoj=khoj, recorder=recorder)

    synced = await deliver()

    assert synced == 0
    assert len(khoj.indexed) == 1  # Khoj was actually called before the failure
    assert outbox.delivered == []
    assert outbox.failed == [{"event_id": event.event_id, "attempts": 1, "error": "RuntimeError"}]


async def test_multiple_events_are_each_synced_independently() -> None:
    good = a_pending(document_id=EXPORT.document_id)
    bad = a_pending()
    outbox = FakeKhojSyncOutbox([good, bad])
    source = FakeKhojExportSource(EXPORT, missing={bad.document_id})
    deliver = _deliver(outbox, source=source)

    synced = await deliver()

    assert synced == 1
    assert outbox.delivered == [good.event_id]
    assert len(outbox.failed) == 1
    assert outbox.failed[0]["event_id"] == bad.event_id


async def test_force_sync_delegates_to_the_enqueuer_and_returns_its_count() -> None:
    workspace_id = uuid.uuid4()
    enqueuer = FakeKhojForceSync(count=3)
    force = ForceKhojSync(enqueuer)

    enqueued = await force(workspace_id)

    assert enqueued == 3
    assert enqueuer.calls == [workspace_id]
