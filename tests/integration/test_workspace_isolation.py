"""Cross-workspace isolation, enforced by the database.

Review found that every foreign key in the derived layer was single-column, so
a row owned by workspace A could reference data owned by workspace B: a
revision could cite another workspace's thought, a journal entry could attach
to another workspace's run, and an idempotency key reused in a second workspace
returned the first workspace's thought.

docs/DESIGN.md 6 says every domain row carries workspace ownership. These tests
assert that the *relationships* carry it too, because a column that nothing
checks is documentation, not isolation.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import CaptureSource, ThoughtDraft, UserId, WorkspaceId
from tc_infrastructure.db.tables import (
    document_revisions,
    documents,
    llm_calls,
    revision_sources,
    runs,
    users,
    workspace_memberships,
    workspaces,
)
from tc_infrastructure.db.thought_repository import PostgresThoughtRepository

pytestmark = pytest.mark.integration


async def a_workspace(
    factory: async_sessionmaker[AsyncSession], name: str
) -> tuple[WorkspaceId, UserId]:
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(sa.insert(users).values(id=user_id, display_name=name))
        await session.execute(
            sa.insert(workspaces).values(
                id=workspace_id,
                name=name,
                mode="personal",
                timezone="Asia/Bangkok",
                digest_local_time=dt.time(20, 0),
            )
        )
        await session.execute(
            sa.insert(workspace_memberships).values(
                workspace_id=workspace_id, user_id=user_id, role="owner"
            )
        )
    return WorkspaceId(workspace_id), UserId(user_id)


@pytest.fixture
async def two_workspaces(
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[tuple[WorkspaceId, UserId], tuple[WorkspaceId, UserId]]:
    return (
        await a_workspace(admin_session_factory, "alpha"),
        await a_workspace(admin_session_factory, "beta"),
    )


def draft(workspace_id: WorkspaceId, user_id: UserId, key: str) -> ThoughtDraft:
    return ThoughtDraft(
        workspace_id=workspace_id,
        author_user_id=user_id,
        source=CaptureSource.API,
        source_message_id=key,
        source_channel_id=None,
        body="synthetic thought",
        client_created_at=dt.datetime(2026, 8, 30, 13, 0, tzinfo=dt.UTC),
        client_timezone="Asia/Bangkok",
        client_local_date=dt.date(2026, 8, 30),
        client_local_time=dt.time(20, 0),
        content_language="en",
        correction_of=None,
    )


# ---------------------------------------------------------------------------
# Idempotency is per workspace
# ---------------------------------------------------------------------------


async def test_the_same_idempotency_key_captures_separately_in_each_workspace(
    app_session_factory: async_sessionmaker[AsyncSession],
    two_workspaces: tuple[tuple[WorkspaceId, UserId], tuple[WorkspaceId, UserId]],
) -> None:
    """The bug this replaces: the second workspace received the first's thought.

    An API caller in workspace B reusing a key that workspace A had already used
    got back A's thought ID and never captured their own.
    """
    (alpha, alpha_user), (beta, beta_user) = two_workspaces
    repository = PostgresThoughtRepository(app_session_factory)
    key = f"shared-{uuid.uuid4()}"

    first = await repository.append(draft(alpha, alpha_user, key))
    second = await repository.append(draft(beta, beta_user, key))

    assert first.created is True
    assert second.created is True, "the second workspace must capture its own thought"
    assert first.thought_id != second.thought_id


async def test_a_lookup_never_crosses_a_workspace(
    app_session_factory: async_sessionmaker[AsyncSession],
    two_workspaces: tuple[tuple[WorkspaceId, UserId], tuple[WorkspaceId, UserId]],
) -> None:
    (alpha, alpha_user), (beta, _) = two_workspaces
    repository = PostgresThoughtRepository(app_session_factory)
    key = f"private-{uuid.uuid4()}"

    await repository.append(draft(alpha, alpha_user, key))

    assert await repository.find_id_by_source_message(beta, CaptureSource.API, key) is None
    assert await repository.find_id_by_source_message(alpha, CaptureSource.API, key) is not None


async def test_redelivery_within_one_workspace_still_deduplicates(
    app_session_factory: async_sessionmaker[AsyncSession],
    two_workspaces: tuple[tuple[WorkspaceId, UserId], tuple[WorkspaceId, UserId]],
) -> None:
    """Scoping the constraint must not weaken it inside a workspace."""
    (alpha, alpha_user), _ = two_workspaces
    repository = PostgresThoughtRepository(app_session_factory)
    key = f"repeat-{uuid.uuid4()}"

    first = await repository.append(draft(alpha, alpha_user, key))
    second = await repository.append(draft(alpha, alpha_user, key))

    assert second.thought_id == first.thought_id
    assert second.created is False


# ---------------------------------------------------------------------------
# Relationships cannot cross a workspace
# ---------------------------------------------------------------------------


async def test_a_revision_cannot_belong_to_another_workspaces_document(
    admin_session_factory: async_sessionmaker[AsyncSession],
    two_workspaces: tuple[tuple[WorkspaceId, UserId], tuple[WorkspaceId, UserId]],
) -> None:
    (alpha, _), (beta, _) = two_workspaces
    document_id = uuid.uuid4()
    run_id = uuid.uuid4()
    body = "## Summary\n\nsynthetic"

    async with admin_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(documents).values(
                id=document_id,
                workspace_id=alpha,
                kind="project",
                stable_key=f"k-{uuid.uuid4()}",
                title="Alpha's document",
            )
        )
        await session.execute(
            sa.insert(runs).values(
                id=run_id, workspace_id=beta, kind="organize", status="succeeded"
            )
        )

    async with admin_session_factory() as session, session.begin():
        with pytest.raises(sa.exc.IntegrityError):
            await session.execute(
                sa.insert(document_revisions).values(
                    id=uuid.uuid4(),
                    document_id=document_id,
                    # Beta claiming ownership of a revision on Alpha's document.
                    workspace_id=beta,
                    parent_revision_id=None,
                    run_id=run_id,
                    revision_number=1,
                    body_markdown=body,
                    body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                    change_summary="synthetic",
                    change_kind="organize",
                )
            )


async def test_a_citation_cannot_reach_into_another_workspace(
    app_session_factory: async_sessionmaker[AsyncSession],
    admin_session_factory: async_sessionmaker[AsyncSession],
    two_workspaces: tuple[tuple[WorkspaceId, UserId], tuple[WorkspaceId, UserId]],
) -> None:
    """The leak that mattered most: provenance pointing at someone else's thought."""
    (alpha, alpha_user), (beta, _) = two_workspaces
    repository = PostgresThoughtRepository(app_session_factory)
    alphas_thought = await repository.append(draft(alpha, alpha_user, f"alpha-{uuid.uuid4()}"))

    document_id = uuid.uuid4()
    run_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    body = "## Summary\n\nbeta's document"

    async with admin_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(documents).values(
                id=document_id,
                workspace_id=beta,
                kind="project",
                stable_key=f"k-{uuid.uuid4()}",
                title="Beta's document",
            )
        )
        await session.execute(
            sa.insert(runs).values(
                id=run_id, workspace_id=beta, kind="organize", status="succeeded"
            )
        )
        await session.execute(
            sa.insert(document_revisions).values(
                id=revision_id,
                document_id=document_id,
                workspace_id=beta,
                run_id=run_id,
                revision_number=1,
                body_markdown=body,
                body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                change_summary="synthetic",
                change_kind="organize",
            )
        )

    async with admin_session_factory() as session, session.begin():
        with pytest.raises(sa.exc.IntegrityError):
            await session.execute(
                sa.insert(revision_sources).values(
                    revision_id=revision_id,
                    thought_id=alphas_thought.thought_id,
                    workspace_id=beta,
                    support_type="direct",
                )
            )


async def test_a_journal_entry_cannot_attach_to_another_workspaces_run(
    admin_session_factory: async_sessionmaker[AsyncSession],
    two_workspaces: tuple[tuple[WorkspaceId, UserId], tuple[WorkspaceId, UserId]],
) -> None:
    """The journal holds raw prompts, so a mis-attached row leaks the most."""
    (alpha, _), (beta, _) = two_workspaces
    run_id = uuid.uuid4()

    async with admin_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(runs).values(id=run_id, workspace_id=alpha, kind="organize", status="running")
        )

    async with admin_session_factory() as session, session.begin():
        with pytest.raises(sa.exc.IntegrityError):
            await session.execute(
                sa.insert(llm_calls).values(
                    id=uuid.uuid4(),
                    workspace_id=beta,
                    run_id=run_id,
                    step="organize",
                    sequence=1,
                    prompt_version="v1",
                    schema_version="v1",
                    model_requested="synthetic/model",
                    request_messages=[],
                    request_params={},
                )
            )


async def test_a_run_cannot_replay_another_workspaces_run(
    admin_session_factory: async_sessionmaker[AsyncSession],
    two_workspaces: tuple[tuple[WorkspaceId, UserId], tuple[WorkspaceId, UserId]],
) -> None:
    (alpha, _), (beta, _) = two_workspaces
    original = uuid.uuid4()

    async with admin_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(runs).values(
                id=original, workspace_id=alpha, kind="organize", status="succeeded"
            )
        )

    async with admin_session_factory() as session, session.begin():
        with pytest.raises(sa.exc.IntegrityError):
            await session.execute(
                sa.insert(runs).values(
                    id=uuid.uuid4(),
                    workspace_id=beta,
                    kind="replay",
                    status="queued",
                    replay_of_run_id=original,
                )
            )


# ---------------------------------------------------------------------------
# Lineage cannot cross documents
# ---------------------------------------------------------------------------


async def test_a_revision_cannot_parent_a_revision_of_another_document(
    admin_session_factory: async_sessionmaker[AsyncSession],
    two_workspaces: tuple[tuple[WorkspaceId, UserId], tuple[WorkspaceId, UserId]],
) -> None:
    """Stricter than workspace scoping: lineage stays inside one document."""
    (alpha, _), _ = two_workspaces
    run_id = uuid.uuid4()
    first_document = uuid.uuid4()
    second_document = uuid.uuid4()
    foreign_revision = uuid.uuid4()
    body = "## Summary\n\nsynthetic"

    async with admin_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(runs).values(
                id=run_id, workspace_id=alpha, kind="organize", status="succeeded"
            )
        )
        for document_id in (first_document, second_document):
            await session.execute(
                sa.insert(documents).values(
                    id=document_id,
                    workspace_id=alpha,
                    kind="project",
                    stable_key=f"k-{uuid.uuid4()}",
                    title="Synthetic",
                )
            )
        await session.execute(
            sa.insert(document_revisions).values(
                id=foreign_revision,
                document_id=first_document,
                workspace_id=alpha,
                run_id=run_id,
                revision_number=1,
                body_markdown=body,
                body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                change_summary="synthetic",
                change_kind="organize",
            )
        )

    async with admin_session_factory() as session, session.begin():
        with pytest.raises(sa.exc.IntegrityError):
            await session.execute(
                sa.insert(document_revisions).values(
                    id=uuid.uuid4(),
                    document_id=second_document,
                    workspace_id=alpha,
                    # A parent belonging to a different document.
                    parent_revision_id=foreign_revision,
                    run_id=run_id,
                    revision_number=1,
                    body_markdown=body,
                    body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                    change_summary="synthetic",
                    change_kind="organize",
                )
            )


# ---------------------------------------------------------------------------
# Checksum integrity
# ---------------------------------------------------------------------------


async def test_a_revision_checksum_must_match_its_body(
    admin_session_factory: async_sessionmaker[AsyncSession],
    two_workspaces: tuple[tuple[WorkspaceId, UserId], tuple[WorkspaceId, UserId]],
) -> None:
    """`rebuild --verify` compares this hash; an unvalidated one proves nothing."""
    (alpha, _), _ = two_workspaces
    run_id = uuid.uuid4()
    document_id = uuid.uuid4()

    async with admin_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(runs).values(
                id=run_id, workspace_id=alpha, kind="organize", status="succeeded"
            )
        )
        await session.execute(
            sa.insert(documents).values(
                id=document_id,
                workspace_id=alpha,
                kind="project",
                stable_key=f"k-{uuid.uuid4()}",
                title="Synthetic",
            )
        )

    async with admin_session_factory() as session, session.begin():
        with pytest.raises(sa.exc.DBAPIError, match="does not match"):
            await session.execute(
                sa.insert(document_revisions).values(
                    id=uuid.uuid4(),
                    document_id=document_id,
                    workspace_id=alpha,
                    run_id=run_id,
                    revision_number=1,
                    body_markdown="the real body",
                    body_sha256="a" * 64,
                    change_summary="synthetic",
                    change_kind="organize",
                )
            )


# ---------------------------------------------------------------------------
# The restore bypass is split
# ---------------------------------------------------------------------------


async def test_the_raw_restore_flag_does_not_unlock_derived_records(
    admin_session_factory: async_sessionmaker[AsyncSession],
    two_workspaces: tuple[tuple[WorkspaceId, UserId], tuple[WorkspaceId, UserId]],
) -> None:
    """Restoring a raw thought must not also permit rewriting the journal.

    Migration 0004 shared one setting between both, so a backup restore
    silently unlocked the immutable audit record too.
    """
    (alpha, _), _ = two_workspaces
    run_id = uuid.uuid4()
    call_id = uuid.uuid4()

    async with admin_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(runs).values(id=run_id, workspace_id=alpha, kind="organize", status="running")
        )
        await session.execute(
            sa.insert(llm_calls).values(
                id=call_id,
                workspace_id=alpha,
                run_id=run_id,
                step="organize",
                sequence=1,
                prompt_version="v1",
                schema_version="v1",
                model_requested="synthetic/model",
                request_messages=[],
                request_params={},
            )
        )

    async with admin_session_factory() as session, session.begin():
        await session.execute(sa.text("SET LOCAL tc.allow_thought_restore = 'on'"))
        with pytest.raises(sa.exc.DBAPIError, match="append-only"):
            await session.execute(
                sa.update(llm_calls)
                .where(llm_calls.c.id == call_id)
                .values(model_requested="rewritten")
            )
