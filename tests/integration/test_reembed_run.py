"""``PostgresReembedRunStore`` against a real database (F15-A, docs/plans/
embedding-sync-review-round-4.md; docs/DESIGN.md 8.5, 9.2, ADR-0010 §3).

Seeding runs through ``admin_session_factory`` (the schema-owning role),
mirroring ``tests/integration/test_document_embeddings.py`` - only the store
under test itself is built against ``app_session_factory``, the same
least-privilege role the running system actually connects as.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import tc_infrastructure.db.reembed_run as reembed_run_module
from tc_infrastructure.db.embedding_sync_enqueue import EMBEDDING_SYNC_REQUESTED_EVENT
from tc_infrastructure.db.reembed_run import PostgresReembedRunStore
from tc_infrastructure.db.tables import (
    EMBEDDING_DIMENSIONS,
    document_embeddings,
    document_revisions,
    documents,
    outbox_events,
    runs,
)

pytestmark = pytest.mark.integration

OLD_MODEL_ID = "thenlper/gte-small@old-revision"
NEW_MODEL_ID = "thenlper/gte-small@new-revision"


def _vector(seed: float) -> list[float]:
    return [seed] * EMBEDDING_DIMENSIONS


async def _document_with_revision(
    session: AsyncSession, workspace_id: uuid.UUID, *, body: str = "synthetic body"
) -> tuple[uuid.UUID, uuid.UUID]:
    document_id = uuid.uuid4()
    run_id = uuid.uuid4()
    revision_id = uuid.uuid4()

    await session.execute(
        sa.insert(runs).values(
            id=run_id, workspace_id=workspace_id, kind="organize", status="succeeded"
        )
    )
    await session.execute(
        sa.insert(documents).values(
            id=document_id,
            workspace_id=workspace_id,
            kind="topic",
            stable_key=f"k-{uuid.uuid4()}",
            title="Synthetic",
        )
    )
    await session.execute(
        sa.insert(document_revisions).values(
            id=revision_id,
            document_id=document_id,
            workspace_id=workspace_id,
            run_id=run_id,
            revision_number=1,
            body_markdown=body,
            body_sha256=hashlib.sha256(body.encode()).hexdigest(),
            change_summary="synthetic",
            change_kind="create",
        )
    )
    # `documents.current_revision_id` -> `document_revisions.id` is not
    # deferrable, so the revision must exist before this column can point
    # to it - the same insert-then-update order organize_writer.py uses in
    # production (packages/infrastructure/.../db/organize_writer.py:89).
    await session.execute(
        sa.update(documents)
        .where(documents.c.id == document_id)
        .values(current_revision_id=revision_id)
    )
    return document_id, revision_id


async def _embed(
    session: AsyncSession, *, document_id: uuid.UUID, revision_id: uuid.UUID, model_id: str
) -> None:
    await session.execute(
        sa.insert(document_embeddings).values(
            document_id=document_id,
            revision_id=revision_id,
            embedding=_vector(0.1),
            embedding_model_id=model_id,
        )
    )


async def _advance_revision(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    workspace_id: uuid.UUID,
    body: str = "synthetic body, revision 2",
) -> uuid.UUID:
    """Commit a fresh revision for `document_id` and repoint
    `documents.current_revision_id` at it - the same insert-then-update
    sequence `organize_writer.py` uses when superseding a document, without
    pulling in an `organize` fixture just for this."""
    run_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    await session.execute(
        sa.insert(runs).values(
            id=run_id, workspace_id=workspace_id, kind="organize", status="succeeded"
        )
    )
    await session.execute(
        sa.insert(document_revisions).values(
            id=revision_id,
            document_id=document_id,
            workspace_id=workspace_id,
            run_id=run_id,
            revision_number=2,
            body_markdown=body,
            body_sha256=hashlib.sha256(body.encode()).hexdigest(),
            change_summary="synthetic supersession",
            change_kind="organize",
        )
    )
    await session.execute(
        sa.update(documents)
        .where(documents.c.id == document_id)
        .values(current_revision_id=revision_id)
    )
    return revision_id


async def _sync_events_for(session: AsyncSession, workspace_id: uuid.UUID) -> list[sa.Row]:
    return (
        await session.execute(
            sa.select(outbox_events).where(
                outbox_events.c.workspace_id == workspace_id,
                outbox_events.c.event_type == EMBEDDING_SYNC_REQUESTED_EVENT,
            )
        )
    ).all()


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


async def test_start_records_a_running_run_with_the_target_model(
    admin_session_factory: async_sessionmaker[AsyncSession],
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = fresh_identity
    async with admin_session_factory() as session, session.begin():
        await _document_with_revision(session, workspace_id)

    store = PostgresReembedRunStore(app_session_factory)
    result = await store.start(workspace_id, embedding_model_id=NEW_MODEL_ID)

    assert result.status == "running"
    assert result.embedding_model_id == NEW_MODEL_ID
    assert result.enqueued == 1

    async with admin_session_factory() as session:
        row = (await session.execute(sa.select(runs).where(runs.c.id == result.run_id))).first()
    assert row is not None
    assert row.kind == "reembed"
    assert row.status == "running"
    assert row.embedding_model_id == NEW_MODEL_ID
    assert row.started_at is not None


async def test_start_deletes_existing_embeddings_and_enqueues_every_current_document(
    admin_session_factory: async_sessionmaker[AsyncSession],
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = fresh_identity
    async with admin_session_factory() as session, session.begin():
        doc_a, rev_a = await _document_with_revision(session, workspace_id, body="a")
        doc_b, rev_b = await _document_with_revision(session, workspace_id, body="b")
        await _embed(session, document_id=doc_a, revision_id=rev_a, model_id=OLD_MODEL_ID)
        await _embed(session, document_id=doc_b, revision_id=rev_b, model_id=OLD_MODEL_ID)

    store = PostgresReembedRunStore(app_session_factory)
    result = await store.start(workspace_id, embedding_model_id=NEW_MODEL_ID)

    assert result.enqueued == 2

    async with admin_session_factory() as session:
        remaining = (
            await session.execute(
                sa.select(document_embeddings.c.document_id).where(
                    document_embeddings.c.document_id.in_([doc_a, doc_b])
                )
            )
        ).all()
        assert remaining == []

        events = await _sync_events_for(session, workspace_id)
    assert len(events) == 2
    enqueued_document_ids = {e.aggregate_id for e in events}
    assert enqueued_document_ids == {str(doc_a), str(doc_b)}


async def test_a_second_start_while_running_is_idempotent(
    admin_session_factory: async_sessionmaker[AsyncSession],
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = fresh_identity
    async with admin_session_factory() as session, session.begin():
        await _document_with_revision(session, workspace_id)

    store = PostgresReembedRunStore(app_session_factory)
    first = await store.start(workspace_id, embedding_model_id=NEW_MODEL_ID)
    second = await store.start(workspace_id, embedding_model_id=NEW_MODEL_ID)

    assert second.run_id == first.run_id
    assert second.status == "running"
    # This call started nothing new - see ReembedRun.enqueued's contract.
    assert second.enqueued == 0

    async with admin_session_factory() as session:
        rows = (
            await session.execute(
                sa.select(runs).where(runs.c.workspace_id == workspace_id, runs.c.kind == "reembed")
            )
        ).all()
    assert len(rows) == 1


async def test_start_is_atomic_when_enqueue_fails_partway_through(
    admin_session_factory: async_sessionmaker[AsyncSession],
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, _ = fresh_identity
    async with admin_session_factory() as session, session.begin():
        doc_a, rev_a = await _document_with_revision(session, workspace_id, body="a")
        doc_b, rev_b = await _document_with_revision(session, workspace_id, body="b")
        await _embed(session, document_id=doc_a, revision_id=rev_a, model_id=OLD_MODEL_ID)
        await _embed(session, document_id=doc_b, revision_id=rev_b, model_id=OLD_MODEL_ID)

    calls = {"count": 0}
    real_enqueue = reembed_run_module.enqueue_embedding_sync

    async def failing_after_first(
        session: AsyncSession, *, workspace_id: uuid.UUID, document_id: uuid.UUID
    ) -> None:
        calls["count"] += 1
        if calls["count"] >= 2:
            raise RuntimeError("synthetic mid-enqueue failure")
        await real_enqueue(session, workspace_id=workspace_id, document_id=document_id)

    monkeypatch.setattr(reembed_run_module, "enqueue_embedding_sync", failing_after_first)

    store = PostgresReembedRunStore(app_session_factory)
    with pytest.raises(RuntimeError, match="synthetic mid-enqueue failure"):
        await store.start(workspace_id, embedding_model_id=NEW_MODEL_ID)

    async with admin_session_factory() as session:
        run_rows = (
            await session.execute(
                sa.select(runs).where(runs.c.workspace_id == workspace_id, runs.c.kind == "reembed")
            )
        ).all()
        assert run_rows == []

        remaining = (
            await session.execute(
                sa.select(document_embeddings.c.document_id).where(
                    document_embeddings.c.document_id.in_([doc_a, doc_b])
                )
            )
        ).all()
        assert {r.document_id for r in remaining} == {doc_a, doc_b}

        events = await _sync_events_for(session, workspace_id)
    assert events == []


async def test_start_never_touches_another_workspaces_embeddings_or_events(
    admin_session_factory: async_sessionmaker[AsyncSession],
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = fresh_identity
    other_workspace_id = uuid.uuid4()
    other_user_id = uuid.uuid4()

    async with admin_session_factory() as session, session.begin():
        await session.execute(
            sa.text("INSERT INTO users (id, display_name) VALUES (:id, :name)"),
            {"id": other_user_id, "name": "other synthetic owner"},
        )
        await session.execute(
            sa.text(
                "INSERT INTO workspaces (id, name, mode, timezone)"
                " VALUES (:id, :name, 'personal', 'Asia/Bangkok')"
            ),
            {"id": other_workspace_id, "name": "other synthetic workspace"},
        )
        await session.execute(
            sa.text(
                "INSERT INTO workspace_memberships (workspace_id, user_id, role)"
                " VALUES (:workspace_id, :user_id, 'owner')"
            ),
            {"workspace_id": other_workspace_id, "user_id": other_user_id},
        )
        doc_mine, rev_mine = await _document_with_revision(session, workspace_id, body="mine")
        doc_other, rev_other = await _document_with_revision(
            session, other_workspace_id, body="other"
        )
        await _embed(session, document_id=doc_mine, revision_id=rev_mine, model_id=OLD_MODEL_ID)
        await _embed(session, document_id=doc_other, revision_id=rev_other, model_id=OLD_MODEL_ID)

    store = PostgresReembedRunStore(app_session_factory)
    await store.start(workspace_id, embedding_model_id=NEW_MODEL_ID)

    async with admin_session_factory() as session:
        other_embedding = (
            await session.execute(
                sa.select(document_embeddings.c.embedding_model_id).where(
                    document_embeddings.c.document_id == doc_other
                )
            )
        ).scalar_one_or_none()
        other_events = await _sync_events_for(session, other_workspace_id)
    assert other_embedding == OLD_MODEL_ID
    assert other_events == []


# ---------------------------------------------------------------------------
# reconciliation primitives
# ---------------------------------------------------------------------------


async def test_count_embedded_under_model_only_counts_current_workspace_and_model(
    admin_session_factory: async_sessionmaker[AsyncSession],
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = fresh_identity
    async with admin_session_factory() as session, session.begin():
        doc_a, rev_a = await _document_with_revision(session, workspace_id, body="a")
        doc_b, rev_b = await _document_with_revision(session, workspace_id, body="b")
        await _embed(session, document_id=doc_a, revision_id=rev_a, model_id=NEW_MODEL_ID)
        await _embed(session, document_id=doc_b, revision_id=rev_b, model_id=OLD_MODEL_ID)

    store = PostgresReembedRunStore(app_session_factory)
    current = await store.count_current_documents(workspace_id)
    embedded = await store.count_embedded_under_model(workspace_id, NEW_MODEL_ID)

    assert current == 2
    assert embedded == 1


async def test_count_embedded_under_model_excludes_a_superseded_revision(
    admin_session_factory: async_sessionmaker[AsyncSession],
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """F18 (docs/plans/embedding-sync-review-round-5.md): a reembed sweep
    enqueues revision r1, the worker embeds r1, then organize commits r2 for
    the same document before the fresh sync event for r2 is delivered. The
    document now has a `document_embeddings` row under the target model, but
    it embeds a revision that is no longer current - `count_embedded_under_model`
    must not count it, or `ReconcileReembedRuns` would mark the run
    `succeeded` while the document's current revision has no embedding under
    the new model at all."""
    workspace_id, _ = fresh_identity
    async with admin_session_factory() as session, session.begin():
        document_id, revision_1 = await _document_with_revision(session, workspace_id)
        await _embed(
            session, document_id=document_id, revision_id=revision_1, model_id=NEW_MODEL_ID
        )
        await _advance_revision(session, document_id=document_id, workspace_id=workspace_id)

    store = PostgresReembedRunStore(app_session_factory)
    current = await store.count_current_documents(workspace_id)
    embedded = await store.count_embedded_under_model(workspace_id, NEW_MODEL_ID)

    assert current == 1
    assert embedded == 0


async def test_count_embedded_under_model_counts_an_embedding_at_the_current_revision(
    admin_session_factory: async_sessionmaker[AsyncSession],
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Mirror of the supersession case above: once the current revision
    itself has been embedded under the target model, it must count - so the
    fix cannot be satisfied by unconditionally returning 0."""
    workspace_id, _ = fresh_identity
    async with admin_session_factory() as session, session.begin():
        document_id, _revision_1 = await _document_with_revision(session, workspace_id)
        revision_2 = await _advance_revision(
            session, document_id=document_id, workspace_id=workspace_id
        )
        await _embed(
            session, document_id=document_id, revision_id=revision_2, model_id=NEW_MODEL_ID
        )

    store = PostgresReembedRunStore(app_session_factory)
    current = await store.count_current_documents(workspace_id)
    embedded = await store.count_embedded_under_model(workspace_id, NEW_MODEL_ID)

    assert current == 1
    assert embedded == 1


async def test_has_dead_lettered_sync_events_true_once_max_attempts_exhausted(
    admin_session_factory: async_sessionmaker[AsyncSession],
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = fresh_identity
    store = PostgresReembedRunStore(app_session_factory)
    reference_time = datetime.now(UTC)

    assert await store.has_dead_lettered_sync_events(workspace_id, since=reference_time) is False

    async with admin_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(outbox_events).values(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                event_type=EMBEDDING_SYNC_REQUESTED_EVENT,
                aggregate_id=str(uuid.uuid4()),
                payload={"document_id": str(uuid.uuid4())},
                attempts=1,
                max_attempts=1,
                created_at=reference_time,
            )
        )

    assert await store.has_dead_lettered_sync_events(workspace_id, since=reference_time) is True


async def test_has_dead_lettered_sync_events_ignores_a_dead_letter_from_before_since(
    admin_session_factory: async_sessionmaker[AsyncSession],
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A stray dead letter from before a reembed run's own `started_at` -
    e.g. the exact F13-A model-conflict halt this feature exists to recover
    from - must never be mistaken for a failure of the run being reconciled
    (round-4 review finding, confirmed by two independent re-reviews)."""
    workspace_id, _ = fresh_identity
    store = PostgresReembedRunStore(app_session_factory)
    run_started_at = datetime.now(UTC)
    stale_dead_letter_created_at = run_started_at - timedelta(days=7)

    async with admin_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(outbox_events).values(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                event_type=EMBEDDING_SYNC_REQUESTED_EVENT,
                aggregate_id=str(uuid.uuid4()),
                payload={"document_id": str(uuid.uuid4())},
                attempts=1,
                max_attempts=1,
                created_at=stale_dead_letter_created_at,
            )
        )

    assert await store.has_dead_lettered_sync_events(workspace_id, since=run_started_at) is False


async def test_has_dead_lettered_sync_events_catches_this_runs_own_dead_letter(
    admin_session_factory: async_sessionmaker[AsyncSession],
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Exercises the real ``start()`` -> ``has_dead_lettered_sync_events``
    path end to end, against genuinely DB-inserted events with their real
    default timestamps - not hand-supplied ``created_at`` values - to prove
    ``runs.started_at`` (set via ``sa.func.now()`` on the same INSERT,
    returned via ``RETURNING``) and ``outbox_events.created_at`` (set via
    that table's own ``DEFAULT now()``) resolve to the same frozen
    transaction timestamp when written inside ``start()``'s one transaction,
    so a dead letter belonging to *this run's own* enqueued sync event is
    still correctly caught by ``created_at >= since`` (round-4 review
    finding: a fix that only excluded stale dead letters, without proving
    this direction still works, would silently make every run's *own*
    terminal failure invisible)."""
    workspace_id, _ = fresh_identity
    async with admin_session_factory() as session, session.begin():
        await _document_with_revision(session, workspace_id)

    store = PostgresReembedRunStore(app_session_factory)
    result = await store.start(workspace_id, embedding_model_id=NEW_MODEL_ID)
    assert result.enqueued == 1

    # Simulate this run's own enqueued sync event exhausting its retries -
    # the real dead-letter path (`PostgresOutbox.mark_failed`) is exercised
    # elsewhere; here only the resulting row shape matters.
    async with admin_session_factory() as session, session.begin():
        events = await _sync_events_for(session, workspace_id)
        assert len(events) == 1
        await session.execute(
            sa.update(outbox_events)
            .where(outbox_events.c.id == events[0].id)
            .values(attempts=events[0].max_attempts)
        )

    assert await store.has_dead_lettered_sync_events(workspace_id, since=result.started_at) is True


async def test_mark_succeeded_and_mark_failed_update_status(
    admin_session_factory: async_sessionmaker[AsyncSession],
    app_session_factory: async_sessionmaker[AsyncSession],
    fresh_identity: tuple[uuid.UUID, uuid.UUID],
) -> None:
    workspace_id, _ = fresh_identity
    store = PostgresReembedRunStore(app_session_factory)
    run = await store.start(workspace_id, embedding_model_id=NEW_MODEL_ID)

    await store.mark_succeeded(run.run_id)
    async with admin_session_factory() as session:
        status = await session.scalar(sa.select(runs.c.status).where(runs.c.id == run.run_id))
        finished_at = await session.scalar(
            sa.select(runs.c.finished_at).where(runs.c.id == run.run_id)
        )
    assert status == "succeeded"
    assert finished_at is not None

    # A second, independent run in the same workspace to exercise
    # mark_failed distinctly.
    other_run_id = uuid.uuid4()
    async with admin_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(runs).values(
                id=other_run_id,
                workspace_id=workspace_id,
                kind="reembed",
                status="running",
                embedding_model_id=NEW_MODEL_ID,
            )
        )

    await store.mark_failed(other_run_id, error_code="embedding_sync_dead_lettered")
    async with admin_session_factory() as session:
        row = (await session.execute(sa.select(runs).where(runs.c.id == other_run_id))).first()
    assert row is not None
    assert row.status == "failed"
    assert row.error_code == "embedding_sync_dead_lettered"
    assert row.finished_at is not None
