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
        # Set only after `_ensure_durable_root` completes its fsync chain
        # without raising. Deliberately per-instance, not derived from
        # `root.exists()`: on-disk existence is not proof the directory
        # entry was ever made durable (see `_ensure_durable_root`).
        self._root_confirmed_durable = False

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
        self._ensure_durable_root()
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
                # Still re-sync the fan-out directories rather than trusting
                # that `exists()` implies durable: a prior write can rename the
                # file and then fail to fsync its directory entry, in which
                # case that write already failed and its caller never got a
                # StoredBlob back. A retry landing here on the same bytes must
                # not report success without confirming durability itself -
                # otherwise the gap the first attempt's failure existed to
                # surface would just be carried forward silently.
                temp_path.unlink(missing_ok=True)
                _fsync_fanout_directories(self._root, storage_key)
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

            # The rename itself must survive a crash. Without this, PostgreSQL
            # can hold - and the bot can acknowledge - an attachment whose
            # directory entry never reached the disk, leaving a committed
            # thought pointing at a blob that does not exist. A never-before-
            # seen prefix creates *two* new fan-out directories, not one, so
            # every level this write may have created is synced, not just the
            # leaf.
            _fsync_fanout_directories(self._root, storage_key)
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

    def _ensure_durable_root(self) -> None:
        """Create the store root (and any missing parents), and confirm it durably.

        ``put`` calls this on every write, but after the first successful call
        in this store's lifetime it is one boolean check and a return - see
        ``self._root_confirmed_durable``.

        This deliberately does **not** use ``self._root.exists()`` as the
        "already done" signal, even though that would make the common case
        just as cheap. A prior call in this same process, or in a process that
        crashed before this one, can have already run ``mkdir`` successfully
        and then had the following fsync raise or never run (process killed,
        power loss). The directory is then present on disk but its entry in
        *its* parent was never confirmed durable - exactly the bug already
        fixed for the fan-out levels one layer down, in
        ``_fsync_fanout_directories``. Checking ``exists()`` here would let
        that unconfirmed state look identical to a confirmed one and be
        silently accepted as success on the very next write, in the same
        process or a fresh one after a restart. A boolean set only once the
        full fsync chain has actually completed without raising cannot be
        fooled that way: if it is ever left ``False``, the next ``put`` call
        redoes the whole check rather than trusting what's on disk.

        ``mkdir(parents=True)`` can create more than one level in one call (a
        root nested under a parent that also doesn't exist yet), so every
        level actually created is walked and synced, not just the leaf.
        """
        if self._root_confirmed_durable:
            return
        root = self._root
        existing_ancestor = root.parent
        while not existing_ancestor.exists():
            existing_ancestor = existing_ancestor.parent
        root.mkdir(parents=True, exist_ok=True)
        directory = root
        while directory != existing_ancestor:
            _fsync_directory(directory.parent)
            directory = directory.parent
        self._root_confirmed_durable = True


def _fsync_fanout_directories(root: Path, storage_key: str) -> None:
    """Flush every directory a write to ``storage_key`` may have created, root included.

    ``storage_key`` is ``ab/cd/<hash>``: two fan-out levels, both possibly
    new. Fsyncing a directory only makes durable the entries *inside* it - it
    says nothing about whether that directory's own entry in its *parent* is
    durable. So a brand-new two-level prefix needs three fsyncs, not two:
    ``root/ab/cd`` (makes the file's entry inside it durable), ``root/ab``
    (makes ``cd``'s entry inside it durable), and ``root`` itself (makes
    ``ab``'s entry inside *it* durable). Stopping at the fan-out levels and
    never syncing ``root`` was the bug: ``ab`` could be fully durable
    internally while root still had no durable record that ``ab`` exists at
    all, and a crash there loses the whole prefix. Every level is synced
    unconditionally rather than tracked as "newly created": fsyncing a
    directory that already existed is cheap, and finding out which levels
    were new would cost a stat call each anyway.
    """
    directory = root / Path(storage_key).parent
    for _ in range(FANOUT_DEPTH + 1):
        _fsync_directory(directory)
        directory = directory.parent


def _fsync_directory(directory: Path) -> None:
    """Flush a directory entry to disk, where the platform supports it.

    POSIX requires an explicit fsync of the *directory* for a rename to be
    durable; syncing the file alone is not enough. Windows has no equivalent
    and rejects opening a directory as a file, so the call is skipped there -
    the development platform, not the deployment platform (docs/DESIGN.md 13).

    On POSIX, a failure here is deliberately **not** swallowed: it propagates
    out of ``put()``, which is still inside its own exception handler and will
    clean up the temporary file and re-raise. Treating this as a harmless,
    debug-logged skip would let the database commit and Discord acknowledge a
    thought whose canonical attachment rename was never made durable - a
    directory entry that a crash before the next fsync can simply lose, with
    nothing left for garbage collection to restore it from.
    """
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
