"""The repository's atomicity and idempotency guarantees, against real PostgreSQL.

``tc_domain.ports.ThoughtRepository`` promises that a thought, its blobs, its
attachment links, and its outbox event commit together or not at all, and that
appending is idempotent on ``(source, source_message_id)``. Those are the
promises the acknowledgement depends on, so they are tested here rather than
inferred from the code.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import (
    ArchivedAttachment,
    CaptureSource,
    Sha256,
    ThoughtDraft,
    UserId,
    WorkspaceId,
)
from tc_infrastructure.db.tables import (
    blobs,
    external_identities,
    outbox_events,
    thought_attachments,
    thoughts,
    users,
    workspace_memberships,
    workspaces,
)
from tc_infrastructure.db.thought_repository import (
    THOUGHT_CAPTURED_EVENT,
    PostgresThoughtRepository,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def seeded(
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[WorkspaceId, UserId]:
    """A committed workspace and owner for thoughts to reference.

    Seeded with the migration role: since migration 0003 the application role
    may only read the identity tables.
    """
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with admin_session_factory() as session, session.begin():
        await session.execute(sa.insert(users).values(id=user_id, display_name="synthetic owner"))
        await session.execute(
            sa.insert(workspaces).values(
                id=workspace_id,
                name="synthetic workspace",
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


def make_draft(
    workspace_id: WorkspaceId,
    user_id: UserId,
    source_message_id: str,
    *,
    body: str = "synthetic thought",
    attachments: tuple[ArchivedAttachment, ...] = (),
) -> ThoughtDraft:
    return ThoughtDraft(
        workspace_id=workspace_id,
        author_user_id=user_id,
        source=CaptureSource.DISCORD,
        source_message_id=source_message_id,
        source_channel_id="channel-1",
        body=body,
        client_created_at=dt.datetime(2026, 8, 30, 13, 0, tzinfo=dt.UTC),
        client_timezone="Asia/Bangkok",
        client_local_date=dt.date(2026, 8, 30),
        client_local_time=dt.time(20, 0),
        content_language="en",
        correction_of=None,
        attachments=attachments,
    )


def make_attachment(marker: str, filename: str = "note.png") -> ArchivedAttachment:
    digest = f"{marker:0>64}"[:64]
    return ArchivedAttachment(
        sha256=Sha256(digest),
        size_bytes=1024,
        media_type="image/png",
        storage_key=f"{digest[:2]}/{digest[2:4]}/{digest}",
        source_filename=filename,
        source_url_expires_at=dt.datetime(2026, 8, 30, 14, 0, tzinfo=dt.UTC),
    )


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------


async def test_append_commits_the_thought(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded: tuple[WorkspaceId, UserId],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = seeded
    repository = PostgresThoughtRepository(app_session_factory)

    outcome = await repository.append(make_draft(workspace_id, user_id, unique_message_id))

    assert outcome.created is True
    async with app_session_factory() as session:
        body = await session.scalar(
            sa.select(thoughts.c.body).where(thoughts.c.id == outcome.thought_id)
        )
    assert body == "synthetic thought"


async def test_append_writes_the_outbox_event_in_the_same_transaction(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded: tuple[WorkspaceId, UserId],
    unique_message_id: str,
) -> None:
    """A committed thought always has a pending acknowledgement (DESIGN.md 6.5)."""
    workspace_id, user_id = seeded
    repository = PostgresThoughtRepository(app_session_factory)

    outcome = await repository.append(make_draft(workspace_id, user_id, unique_message_id))

    async with app_session_factory() as session:
        event = (
            await session.execute(
                sa.select(outbox_events).where(
                    outbox_events.c.aggregate_id == str(outcome.thought_id),
                    outbox_events.c.event_type == THOUGHT_CAPTURED_EVENT,
                )
            )
        ).one()

    assert event.delivered_at is None
    assert event.attempts == 0
    assert event.payload["thought_id"] == outcome.thought_id
    # Personal content must not be copied into a queue that is logged and retried.
    assert "body" not in event.payload


async def test_append_links_attachments_and_blobs(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded: tuple[WorkspaceId, UserId],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = seeded
    repository = PostgresThoughtRepository(app_session_factory)
    attachment = make_attachment(uuid.uuid4().hex)

    outcome = await repository.append(
        make_draft(workspace_id, user_id, unique_message_id, attachments=(attachment,))
    )

    async with app_session_factory() as session:
        link = (
            await session.execute(
                sa.select(thought_attachments).where(
                    thought_attachments.c.thought_id == outcome.thought_id
                )
            )
        ).one()
        blob = (
            await session.execute(sa.select(blobs).where(blobs.c.sha256 == attachment.sha256))
        ).one()

    assert link.source_filename == "note.png"
    assert link.extracted_status == "not_supported"
    assert blob.storage_key == attachment.storage_key


async def test_the_same_blob_can_back_two_thoughts(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded: tuple[WorkspaceId, UserId],
) -> None:
    """Content addressing means identical bytes are stored once, referenced twice."""
    workspace_id, user_id = seeded
    repository = PostgresThoughtRepository(app_session_factory)
    attachment = make_attachment(uuid.uuid4().hex)

    first = await repository.append(
        make_draft(workspace_id, user_id, f"blob-a-{uuid.uuid4()}", attachments=(attachment,))
    )
    second = await repository.append(
        make_draft(workspace_id, user_id, f"blob-b-{uuid.uuid4()}", attachments=(attachment,))
    )

    async with app_session_factory() as session:
        blob_count = await session.scalar(
            sa.select(sa.func.count()).select_from(blobs).where(blobs.c.sha256 == attachment.sha256)
        )
        link_count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(thought_attachments)
            .where(thought_attachments.c.thought_id.in_([first.thought_id, second.thought_id]))
        )

    assert blob_count == 1
    assert link_count == 2


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_redelivery_returns_the_existing_thought(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded: tuple[WorkspaceId, UserId],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = seeded
    repository = PostgresThoughtRepository(app_session_factory)
    draft = make_draft(workspace_id, user_id, unique_message_id)

    first = await repository.append(draft)
    second = await repository.append(draft)

    assert second.thought_id == first.thought_id
    assert second.created is False

    async with app_session_factory() as session:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(thoughts)
            .where(thoughts.c.source_message_id == unique_message_id)
        )
    assert count == 1


async def test_redelivery_does_not_enqueue_a_second_acknowledgement(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded: tuple[WorkspaceId, UserId],
    unique_message_id: str,
) -> None:
    """Otherwise a Discord retry storm would produce a storm of replies."""
    workspace_id, user_id = seeded
    repository = PostgresThoughtRepository(app_session_factory)
    draft = make_draft(workspace_id, user_id, unique_message_id)

    outcome = await repository.append(draft)
    await repository.append(draft)
    await repository.append(draft)

    async with app_session_factory() as session:
        events = await session.scalar(
            sa.select(sa.func.count())
            .select_from(outbox_events)
            .where(outbox_events.c.aggregate_id == str(outcome.thought_id))
        )
    assert events == 1


async def test_find_id_by_source_message(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded: tuple[WorkspaceId, UserId],
    unique_message_id: str,
) -> None:
    workspace_id, user_id = seeded
    repository = PostgresThoughtRepository(app_session_factory)

    assert (
        await repository.find_id_by_source_message(
            workspace_id, CaptureSource.DISCORD, unique_message_id
        )
        is None
    )

    outcome = await repository.append(make_draft(workspace_id, user_id, unique_message_id))

    found = await repository.find_id_by_source_message(
        workspace_id, CaptureSource.DISCORD, unique_message_id
    )
    assert found == outcome.thought_id


# ---------------------------------------------------------------------------
# Atomicity under failure
# ---------------------------------------------------------------------------


async def test_a_failed_attachment_link_commits_no_thought(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded: tuple[WorkspaceId, UserId],
    unique_message_id: str,
) -> None:
    """Fault injection: the thought inserts, then the attachment link explodes.

    Nothing may survive. A committed thought whose attachment is missing would
    be an acknowledged capture with lost data, which is the one outcome the
    design forbids outright (docs/DESIGN.md 7.1).
    """
    workspace_id, user_id = seeded
    repository = PostgresThoughtRepository(app_session_factory)

    # An oversized digest violates char(64); the failure lands mid-transaction,
    # after the thought row has been inserted.
    poisoned = ArchivedAttachment(
        sha256=Sha256("f" * 65),
        size_bytes=1,
        media_type="image/png",
        storage_key="ff/ff/" + "f" * 65,
        source_filename="poison.png",
    )

    with pytest.raises(sa.exc.DBAPIError):
        await repository.append(
            make_draft(workspace_id, user_id, unique_message_id, attachments=(poisoned,))
        )

    async with app_session_factory() as session:
        thought_count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(thoughts)
            .where(thoughts.c.source_message_id == unique_message_id)
        )
        event_count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(outbox_events)
            .where(outbox_events.c.aggregate_id == unique_message_id)
        )

    assert thought_count == 0, "the thought must not survive a failed attachment link"
    assert event_count == 0, "no acknowledgement may be pending for a rolled-back capture"


async def test_a_thought_for_an_unknown_workspace_is_rejected(
    app_session_factory: async_sessionmaker[AsyncSession],
    unique_message_id: str,
) -> None:
    """Workspace scoping is enforced by foreign key, not by hope."""
    repository = PostgresThoughtRepository(app_session_factory)
    draft = make_draft(WorkspaceId(uuid.uuid4()), UserId(uuid.uuid4()), unique_message_id)

    with pytest.raises(sa.exc.IntegrityError):
        await repository.append(draft)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def test_external_identity_maps_discord_user_to_workspace(
    app_session_factory: async_sessionmaker[AsyncSession],
    admin_session_factory: async_sessionmaker[AsyncSession],
    seeded: tuple[WorkspaceId, UserId],
) -> None:
    """The bootstrap role writes the mapping; the capture role reads it."""
    workspace_id, user_id = seeded
    discord_id = str(uuid.uuid4().int)[:18]

    async with admin_session_factory() as session, session.begin():
        await session.execute(
            sa.insert(external_identities).values(
                workspace_id=workspace_id,
                user_id=user_id,
                provider="discord",
                external_user_id=discord_id,
            )
        )

    async with app_session_factory() as session:
        row = (
            await session.execute(
                sa.select(external_identities).where(
                    external_identities.c.provider == "discord",
                    external_identities.c.external_user_id == discord_id,
                )
            )
        ).one()

    assert row.workspace_id == workspace_id


async def test_the_capture_role_cannot_create_a_workspace(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Least privilege: minting workspaces is a bootstrap task (migration 0003)."""
    async with app_session_factory() as session, session.begin():
        with pytest.raises(sa.exc.ProgrammingError, match="permission denied"):
            await session.execute(
                sa.insert(workspaces).values(
                    id=uuid.uuid4(),
                    name="unauthorised",
                    mode="personal",
                    timezone="Asia/Bangkok",
                    digest_local_time=dt.time(20, 0),
                )
            )
