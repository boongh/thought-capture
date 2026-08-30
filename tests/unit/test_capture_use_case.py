"""Behaviour of the capture use case.

The guarantees under test are the ones Release 1 is defined by: a redelivered
Discord message creates no duplicate thought, and nothing is acknowledged that
was not committed (docs/DESIGN.md 1.1).
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from tc_application.capture import CaptureThought
from tc_domain.capture import (
    AttachmentCandidate,
    CaptureCommand,
    CaptureSource,
    UserId,
    WorkspaceId,
)
from tc_domain.errors import (
    AttachmentArchiveFailed,
    AttachmentRejected,
    CaptureRejected,
    InvalidCaptureCommand,
)
from tc_domain.policy import AttachmentPolicy
from tests.unit.fakes import FakeAttachmentArchive, FakeThoughtRepository

WORKSPACE_ID = WorkspaceId(uuid.UUID("11111111-1111-4111-8111-111111111111"))
USER_ID = UserId(uuid.UUID("22222222-2222-4222-8222-222222222222"))
BANGKOK = "Asia/Bangkok"


def make_command(
    *,
    source_message_id: str = "msg-1",
    body: str = "remember to renew the passport",
    attachments: tuple[AttachmentCandidate, ...] = (),
    client_created_at: dt.datetime | None = None,
    timezone: str = BANGKOK,
) -> CaptureCommand:
    return CaptureCommand(
        workspace_id=WORKSPACE_ID,
        author_user_id=USER_ID,
        source=CaptureSource.DISCORD,
        source_message_id=source_message_id,
        body=body,
        client_created_at=client_created_at or dt.datetime(2026, 8, 30, 13, 0, tzinfo=dt.UTC),
        client_timezone=timezone,
        source_channel_id="channel-1",
        attachments=attachments,
    )


def make_use_case(
    repository: FakeThoughtRepository | None = None,
    archive: FakeAttachmentArchive | None = None,
    *,
    max_bytes: int = 25 * 1024 * 1024,
) -> tuple[CaptureThought, FakeThoughtRepository, FakeAttachmentArchive]:
    repo = repository or FakeThoughtRepository()
    arch = archive or FakeAttachmentArchive()
    use_case = CaptureThought(repo, arch, AttachmentPolicy(max_bytes=max_bytes))
    return use_case, repo, arch


def image(filename: str = "note.png", size_bytes: int = 1024) -> AttachmentCandidate:
    return AttachmentCandidate(
        filename=filename,
        media_type="image/png",
        size_bytes=size_bytes,
        url=f"https://cdn.example.invalid/{filename}?signature=abc",
        url_expires_at=dt.datetime(2026, 8, 30, 14, 0, tzinfo=dt.UTC),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_text_message_is_captured() -> None:
    use_case, repo, _ = make_use_case()

    result = await use_case.execute(make_command())

    assert result.deduplicated is False
    assert result.attachment_count == 0
    assert len(repo.appended) == 1
    assert repo.appended[0].body == "remember to renew the passport"


async def test_local_calendar_fields_are_derived_from_the_client_timezone() -> None:
    """13:00 UTC is 20:00 in Bangkok - the same day, but only just."""
    use_case, repo, _ = make_use_case()

    await use_case.execute(
        make_command(client_created_at=dt.datetime(2026, 8, 30, 13, 0, tzinfo=dt.UTC))
    )

    draft = repo.appended[0]
    assert draft.client_local_date == dt.date(2026, 8, 30)
    assert draft.client_local_time == dt.time(20, 0)


async def test_late_evening_utc_belongs_to_the_next_local_day() -> None:
    """18:00 UTC is 01:00 the following day in Bangkok.

    Getting this wrong would file a thought under the wrong day and make
    date-scoped search quietly miss it.
    """
    use_case, repo, _ = make_use_case()

    await use_case.execute(
        make_command(client_created_at=dt.datetime(2026, 8, 30, 18, 0, tzinfo=dt.UTC))
    )

    draft = repo.appended[0]
    assert draft.client_local_date == dt.date(2026, 8, 31)
    assert draft.client_local_time == dt.time(1, 0)


async def test_attachments_are_archived_and_linked() -> None:
    use_case, repo, archive = make_use_case()

    result = await use_case.execute(make_command(attachments=(image(), image("second.png"))))

    assert result.attachment_count == 2
    assert len(archive.archived) == 2
    draft = repo.appended[0]
    assert {a.source_filename for a in draft.attachments} == {"note.png", "second.png"}
    # Release 1 archives only; it never claims to understand the content.
    assert all(a.extracted_status == "not_supported" for a in draft.attachments)


async def test_message_with_only_an_attachment_is_captured() -> None:
    use_case, repo, _ = make_use_case()

    result = await use_case.execute(make_command(body="", attachments=(image(),)))

    assert result.attachment_count == 1
    assert repo.appended[0].body == ""


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_redelivery_returns_the_existing_thought() -> None:
    use_case, repo, _ = make_use_case()
    command = make_command(attachments=(image(),))

    first = await use_case.execute(command)
    second = await use_case.execute(command)

    assert second.thought_id == first.thought_id
    assert second.deduplicated is True
    assert len(repo.appended) == 1, "a redelivery must not append a second row"


async def test_redelivery_does_not_re_download_attachments() -> None:
    """Bandwidth and provider rate limits matter; a retry storm must stay cheap."""
    use_case, _, archive = make_use_case()
    command = make_command(attachments=(image(),))

    await use_case.execute(command)
    await use_case.execute(command)

    assert len(archive.archived) == 1


async def test_concurrent_delivery_loses_the_race_gracefully() -> None:
    """The lookup misses but the append conflicts, as two workers would race.

    The unique constraint, not the lookup, is the real guarantee.
    """
    use_case, repo, _ = make_use_case()
    command = make_command()
    first = await use_case.execute(command)

    repo.hide_from_lookup = True
    second = await use_case.execute(command)

    assert second.thought_id == first.thought_id
    assert second.deduplicated is True
    assert len(repo.appended) == 1


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------


async def test_empty_message_is_rejected() -> None:
    use_case, repo, _ = make_use_case()

    with pytest.raises(CaptureRejected):
        await use_case.execute(make_command(body="   "))

    assert repo.appended == []


async def test_oversized_attachment_is_rejected_before_any_download() -> None:
    use_case, repo, archive = make_use_case(max_bytes=1000)

    with pytest.raises(AttachmentRejected, match="exceeds"):
        await use_case.execute(make_command(attachments=(image(size_bytes=5000),)))

    assert archive.archived == [], "policy must be applied before bytes are fetched"
    assert repo.appended == []


async def test_blocked_extension_is_rejected() -> None:
    use_case, _, archive = make_use_case()
    payload = AttachmentCandidate(
        filename="totally-safe.exe",
        media_type="application/octet-stream",
        size_bytes=100,
        url="https://cdn.example.invalid/totally-safe.exe",
    )

    with pytest.raises(AttachmentRejected, match="extension"):
        await use_case.execute(make_command(attachments=(payload,)))

    assert archive.archived == []


async def test_failed_attachment_archive_fails_the_whole_capture() -> None:
    """Release 1 treats one message as one atomic capture (docs/DESIGN.md 7.1)."""
    use_case, repo, _ = make_use_case(archive=FakeAttachmentArchive(fail_on="broken.png"))

    with pytest.raises(AttachmentArchiveFailed):
        await use_case.execute(make_command(attachments=(image(), image("broken.png"))))

    assert repo.appended == [], "no thought may be committed if an attachment was lost"


def test_naive_timestamp_is_rejected_at_construction() -> None:
    with pytest.raises(InvalidCaptureCommand, match="timezone-aware"):
        make_command(client_created_at=dt.datetime(2026, 8, 30, 13, 0))  # noqa: DTZ001


def test_unknown_timezone_is_rejected_at_construction() -> None:
    with pytest.raises(InvalidCaptureCommand, match="IANA"):
        make_command(timezone="Mars/Olympus_Mons")


def test_missing_source_message_id_is_rejected() -> None:
    with pytest.raises(InvalidCaptureCommand, match="idempotency"):
        make_command(source_message_id="")
