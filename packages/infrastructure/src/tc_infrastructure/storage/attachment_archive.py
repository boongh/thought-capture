"""Copies attachments from their expiring source URLs into durable storage.

Discord attachment URLs are signed and expire, so the bytes must be copied
during ingestion or they are lost (docs/DESIGN.md 4.1). Release 1 archives only:
no OCR, no vision inference, no transcription.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from tc_domain.capture import ArchivedAttachment, AttachmentCandidate, ExtractedStatus, Sha256
from tc_domain.errors import AttachmentArchiveFailed
from tc_infrastructure.storage.blob_store import FilesystemBlobStore

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT_SECONDS = 30.0
CHUNK_BYTES = 64 * 1024


class HttpAttachmentArchive:
    """Streams an attachment over HTTP into the content-addressed blob store."""

    def __init__(
        self,
        blob_store: FilesystemBlobStore,
        client: httpx.AsyncClient,
        *,
        max_bytes: int,
    ) -> None:
        self._blobs = blob_store
        self._client = client
        self._max_bytes = max_bytes

    async def archive(self, candidate: AttachmentCandidate) -> ArchivedAttachment:
        try:
            stored = await self._blobs.put(self._download(candidate))
        except AttachmentArchiveFailed:
            raise
        except httpx.HTTPError as exc:
            # Never include the URL: it carries a signature that grants access.
            raise AttachmentArchiveFailed(
                f"could not download {candidate.filename!r}: {type(exc).__name__}"
            ) from exc
        except OSError as exc:
            raise AttachmentArchiveFailed(
                f"could not store {candidate.filename!r}: {type(exc).__name__}"
            ) from exc

        logger.info(
            "attachment.archived",
            extra={
                "sha256": stored.sha256,
                "size_bytes": stored.size_bytes,
                "deduplicated": stored.deduplicated,
            },
        )

        return ArchivedAttachment(
            sha256=Sha256(stored.sha256),
            size_bytes=stored.size_bytes,
            media_type=candidate.media_type,
            storage_key=stored.storage_key,
            source_filename=candidate.filename,
            source_url_expires_at=candidate.url_expires_at,
            extracted_status=ExtractedStatus.NOT_SUPPORTED,
        )

    async def _download(self, candidate: AttachmentCandidate) -> AsyncIterator[bytes]:
        async with self._client.stream(
            "GET", candidate.url, timeout=DOWNLOAD_TIMEOUT_SECONDS
        ) as response:
            response.raise_for_status()

            received = 0
            async for chunk in response.aiter_bytes(CHUNK_BYTES):
                received += len(chunk)
                # The size the source *declared* was already checked by policy.
                # This checks the size actually delivered, so a source that lies
                # cannot make us write an unbounded file to disk.
                if received > self._max_bytes:
                    raise AttachmentArchiveFailed(
                        f"{candidate.filename!r} exceeded the {self._max_bytes} byte limit "
                        "while downloading"
                    )
                yield chunk
