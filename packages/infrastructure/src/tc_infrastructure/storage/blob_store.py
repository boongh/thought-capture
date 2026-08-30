"""Content-addressed blob storage on the local filesystem.

Objects are named by the SHA-256 of their contents, so storing the same bytes
twice is a no-op and a partially-written object can never be mistaken for a
complete one. Original attachments are canonical data (CLAUDE.md), so writes are
atomic and existing objects are never overwritten.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Two levels of fan-out. A single flat directory degrades badly on some
# filesystems once it holds tens of thousands of entries.
FANOUT_DEPTH = 2
FANOUT_WIDTH = 2


@dataclass(frozen=True, slots=True)
class StoredBlob:
    sha256: str
    size_bytes: int
    storage_key: str
    deduplicated: bool


def storage_key_for(sha256: str) -> str:
    """``ab/cd/abcd...`` - stable, and derivable from the hash alone."""
    parts = [sha256[i * FANOUT_WIDTH : (i + 1) * FANOUT_WIDTH] for i in range(FANOUT_DEPTH)]
    return "/".join([*parts, sha256])


class FilesystemBlobStore:
    """Stores blobs under ``root`` by content hash."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, storage_key: str) -> Path:
        return self._root / storage_key

    async def put(self, chunks: AsyncIterator[bytes]) -> StoredBlob:
        """Stream ``chunks`` to disk, hashing as we go, then place atomically.

        The hash is only known once the whole stream has been read, so the data
        is written to a temporary file first. The temporary file is created in
        the store's own directory so that the final ``os.replace`` stays within
        one filesystem, where it is atomic.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0

        temp_fd, temp_name = tempfile.mkstemp(dir=self._root, prefix=".incoming-")
        temp_path = Path(temp_name)
        try:
            with os.fdopen(temp_fd, "wb") as handle:
                async for chunk in chunks:
                    digest.update(chunk)
                    size += len(chunk)
                    handle.write(chunk)
                handle.flush()
                # Durability before the rename: a rename that survives a crash
                # while its contents do not would leave a corrupt object under
                # a hash that claims otherwise.
                os.fsync(handle.fileno())

            sha256 = digest.hexdigest()
            storage_key = storage_key_for(sha256)
            final_path = self.path_for(storage_key)
            final_path.parent.mkdir(parents=True, exist_ok=True)

            if final_path.exists():
                # Same bytes already stored. Keep the original and discard ours.
                temp_path.unlink(missing_ok=True)
                logger.debug("blob.deduplicated", extra={"sha256": sha256, "size_bytes": size})
                return StoredBlob(
                    sha256=sha256,
                    size_bytes=size,
                    storage_key=storage_key,
                    deduplicated=True,
                )

            # Atomic within a single filesystem, which is why the temporary file
            # was created inside the store's own directory.
            temp_path.replace(final_path)
            logger.info("blob.stored", extra={"sha256": sha256, "size_bytes": size})
            return StoredBlob(
                sha256=sha256, size_bytes=size, storage_key=storage_key, deduplicated=False
            )
        except BaseException:
            # Includes cancellation: a half-written temp file must not be left
            # behind for the garbage collector to puzzle over.
            temp_path.unlink(missing_ok=True)
            raise

    def open_stream(self, storage_key: str) -> bytes:
        """Read a stored blob back. Used by export and restore verification."""
        return self.path_for(storage_key).read_bytes()
