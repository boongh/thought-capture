"""HTTP attachment archiving.

Discord attachment URLs are signed and expire, so ingestion must copy the bytes
or lose them. Failures here must fail the capture rather than produce a thought
with a missing attachment (docs/DESIGN.md 7.1).
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import httpx
import pytest

from tc_domain.attachment_origin import AttachmentOriginPolicy
from tc_domain.capture import AttachmentCandidate
from tc_domain.errors import AttachmentArchiveFailed
from tc_infrastructure.storage.attachment_archive import HttpAttachmentArchive
from tc_infrastructure.storage.blob_store import FilesystemBlobStore

SIGNED_URL = "https://cdn.example.invalid/note.png?ex=abc&is=def&hm=signature"


def candidate(filename: str = "note.png", size_bytes: int = 12) -> AttachmentCandidate:
    return AttachmentCandidate(
        filename=filename,
        media_type="image/png",
        size_bytes=size_bytes,
        url=SIGNED_URL,
        url_expires_at=dt.datetime(2026, 8, 30, 14, 0, tzinfo=dt.UTC),
    )


def archive_with(
    tmp_path: Path, handler: httpx.MockTransport, *, max_bytes: int = 1024
) -> tuple[HttpAttachmentArchive, FilesystemBlobStore, httpx.AsyncClient]:
    store = FilesystemBlobStore(tmp_path)
    client = httpx.AsyncClient(transport=handler)
    # The fake CDN is allowlisted explicitly; the policy itself is tested in
    # tests/unit/test_attachment_origin.py.
    policy = AttachmentOriginPolicy(allowed_hosts=frozenset({"cdn.example.invalid"}))
    archive = HttpAttachmentArchive(store, client, max_bytes=max_bytes, origin_policy=policy)
    return archive, store, client


async def test_archives_bytes_into_content_addressed_storage(tmp_path: Path) -> None:
    payload = b"synthetic png"
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    archive, store, client = archive_with(tmp_path, transport)

    async with client:
        result = await archive.archive(candidate())

    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.size_bytes == len(payload)
    assert result.source_filename == "note.png"
    # Release 1 archives only; it never claims to understand the content.
    assert result.extracted_status == "not_supported"
    assert store.path_for(result.storage_key).read_bytes() == payload


async def test_expiry_is_recorded_for_provenance(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"x"))
    archive, _, client = archive_with(tmp_path, transport)

    async with client:
        result = await archive.archive(candidate())

    assert result.source_url_expires_at == dt.datetime(2026, 8, 30, 14, 0, tzinfo=dt.UTC)


async def test_http_error_fails_the_capture(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(404))
    archive, _, client = archive_with(tmp_path, transport)

    async with client:
        with pytest.raises(AttachmentArchiveFailed):
            await archive.archive(candidate())


async def test_failure_message_never_leaks_the_signed_url(tmp_path: Path) -> None:
    """The URL carries a signature that grants access to the object."""
    transport = httpx.MockTransport(lambda _: httpx.Response(403))
    archive, _, client = archive_with(tmp_path, transport)

    async with client:
        with pytest.raises(AttachmentArchiveFailed) as caught:
            await archive.archive(candidate())

    message = str(caught.value)
    assert "signature" not in message
    assert "hm=" not in message
    assert "note.png" in message


async def test_a_source_that_lies_about_size_is_cut_off(tmp_path: Path) -> None:
    """Policy checks the declared size; this checks the delivered size.

    Without it, a source could declare one kilobyte and stream unbounded data
    onto the disk.
    """
    oversized = b"x" * 5000
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=oversized))
    archive, _, client = archive_with(tmp_path, transport, max_bytes=1024)

    async with client:
        with pytest.raises(AttachmentArchiveFailed, match="while downloading"):
            await archive.archive(candidate(size_bytes=10))

    leftovers = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert leftovers == [], "an aborted oversized download left files behind"


async def test_identical_attachments_are_stored_once(tmp_path: Path) -> None:
    payload = b"same bytes"
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    archive, _, client = archive_with(tmp_path, transport)

    async with client:
        first = await archive.archive(candidate("a.png"))
        second = await archive.archive(candidate("b.png"))

    assert first.sha256 == second.sha256
    assert first.storage_key == second.storage_key
    stored_files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(stored_files) == 1
