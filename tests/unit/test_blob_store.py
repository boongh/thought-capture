"""Content-addressed storage behaviour.

Original attachments are canonical data (CLAUDE.md), so the store must never
overwrite an existing object, never leave a partial file where a complete one is
expected, and never leave litter behind on failure.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tc_infrastructure.storage import blob_store
from tc_infrastructure.storage.blob_store import FilesystemBlobStore, storage_key_for


async def chunks_of(*payloads: bytes) -> AsyncIterator[bytes]:
    for payload in payloads:
        yield payload


async def exploding_chunks(before: bytes) -> AsyncIterator[bytes]:
    yield before
    raise OSError("synthetic transport failure mid-stream")


def test_storage_key_fans_out_by_hash_prefix() -> None:
    digest = "abcdef" + "0" * 58
    assert storage_key_for(digest) == f"ab/cd/{digest}"


async def test_put_stores_by_content_hash(tmp_path: Path) -> None:
    store = FilesystemBlobStore(tmp_path)
    payload = b"synthetic attachment bytes"

    stored = await store.put(chunks_of(payload))

    assert stored.sha256 == hashlib.sha256(payload).hexdigest()
    assert stored.size_bytes == len(payload)
    assert stored.deduplicated is False
    assert store.path_for(stored.storage_key).read_bytes() == payload


async def test_put_reassembles_a_chunked_stream(tmp_path: Path) -> None:
    store = FilesystemBlobStore(tmp_path)

    stored = await store.put(chunks_of(b"first ", b"second ", b"third"))

    assert store.path_for(stored.storage_key).read_bytes() == b"first second third"
    assert stored.sha256 == hashlib.sha256(b"first second third").hexdigest()


async def test_storing_identical_bytes_twice_is_idempotent(tmp_path: Path) -> None:
    store = FilesystemBlobStore(tmp_path)
    payload = b"the same bytes"

    first = await store.put(chunks_of(payload))
    second = await store.put(chunks_of(payload))

    assert second.sha256 == first.sha256
    assert second.storage_key == first.storage_key
    assert first.deduplicated is False
    assert second.deduplicated is True


async def test_an_existing_object_is_never_overwritten(tmp_path: Path) -> None:
    """Guards the canonical guarantee: stored bytes are immutable."""
    store = FilesystemBlobStore(tmp_path)
    payload = b"original bytes"
    stored = await store.put(chunks_of(payload))
    path = store.path_for(stored.storage_key)
    mtime_before = path.stat().st_mtime_ns

    await store.put(chunks_of(payload))

    assert path.read_bytes() == payload
    assert path.stat().st_mtime_ns == mtime_before, "the object was rewritten"


async def test_a_failed_stream_leaves_no_object_and_no_litter(tmp_path: Path) -> None:
    """A partial download must not become a blob, nor a stray temp file."""
    store = FilesystemBlobStore(tmp_path)

    with pytest.raises(OSError, match="synthetic transport failure"):
        await store.put(exploding_chunks(b"partial"))

    leftovers = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert leftovers == [], f"failed write left files behind: {leftovers}"


async def test_empty_payload_is_stored_faithfully(tmp_path: Path) -> None:
    """A zero-byte attachment is unusual but not an error."""
    store = FilesystemBlobStore(tmp_path)

    stored = await store.put(chunks_of())

    assert stored.size_bytes == 0
    assert stored.sha256 == hashlib.sha256(b"").hexdigest()
    assert store.path_for(stored.storage_key).read_bytes() == b""


async def test_a_directory_fsync_failure_fails_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename that cannot be made durable must not be reported as success.

    Swallowing this error (as a debug-logged skip) let the database commit and
    Discord acknowledge an attachment whose canonical rename was never
    guaranteed to survive a crash. It must fail the write instead.
    """

    def exploding_fsync_directory(directory: Path) -> None:
        raise OSError("synthetic directory fsync failure")

    monkeypatch.setattr(blob_store, "_fsync_directory", exploding_fsync_directory)
    store = FilesystemBlobStore(tmp_path)

    with pytest.raises(OSError, match="synthetic directory fsync failure"):
        await store.put(chunks_of(b"payload"))


async def test_every_fanout_directory_level_is_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brand-new two-character prefix creates two directories, not one - and
    ``root`` needs its own fsync too, or its record that either exists is not durable.

    Fsyncing a directory only makes durable the entries *inside* it. Syncing
    just the leaf and its immediate parent confirms the file's entry and the
    leaf's entry are durable, but not that the leaf's parent's entry is
    durable *inside root* - so root must be synced too, not just the two
    fan-out levels below it.

    Uses a relative root (via ``monkeypatch.chdir``) specifically so the
    exact-equality assertion below stays bounded at the working directory -
    an absolute root's root-confirmation walk is bounded at the drive root
    instead (see ``_ensure_durable_root``), which for a real filesystem path
    like ``tmp_path`` would make an exact list fragile and environment-sized.
    """
    monkeypatch.chdir(tmp_path)
    payload = b"a payload under a brand-new fan-out prefix"
    expected_key = storage_key_for(hashlib.sha256(payload).hexdigest())
    synced: list[Path] = []
    monkeypatch.setattr(blob_store, "_fsync_directory", lambda directory: synced.append(directory))
    root = Path("attachments")
    store = FilesystemBlobStore(root)

    stored = await store.put(chunks_of(payload))

    assert stored.storage_key == expected_key
    leaf = root / Path(expected_key).parent
    # The leading entry is the once-per-lifetime root-durability confirmation
    # (see test_an_existing_root_is_confirmed_once_per_store_lifetime_not_every_write),
    # which runs before any fan-out directory is touched.
    assert synced == [Path(), leaf, leaf.parent, root]


async def test_a_nonexistent_root_is_created_durably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean install's configured root (``./attachments`` by default) does not exist yet.

    ``mkdir(parents=True)`` alone creates the directory but does not make its
    entry in *its own* parent durable - a power loss right after the first
    acknowledged attachment could take the whole freshly-created store with
    it, the same bug fixed for the fan-out levels but one level higher.
    """
    root = tmp_path / "attachments"
    payload = b"the first attachment this store has ever seen"
    synced: list[Path] = []
    monkeypatch.setattr(blob_store, "_fsync_directory", lambda directory: synced.append(directory))
    store = FilesystemBlobStore(root)

    await store.put(chunks_of(payload))

    assert root.is_dir()
    assert tmp_path in synced, "root's own entry in its parent was never made durable"


async def test_a_multi_level_nonexistent_root_syncs_every_new_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mkdir(parents=True)`` can create more than one new directory in one call."""
    root = tmp_path / "data" / "attachments"
    payload = b"a payload under a root nested two levels deep"
    synced: list[Path] = []
    monkeypatch.setattr(blob_store, "_fsync_directory", lambda directory: synced.append(directory))
    store = FilesystemBlobStore(root)

    await store.put(chunks_of(payload))

    assert root.is_dir()
    assert tmp_path / "data" in synced, "the intermediate 'data' directory was not synced"
    assert tmp_path in synced, "'data''s own entry inside tmp_path was not synced"


async def test_an_existing_root_is_confirmed_once_per_store_lifetime_not_every_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On-disk existence is never trusted as proof of durability on its own -
    see ``_ensure_durable_root`` - so a fresh store instance confirms root's
    own entry in its parent once, even though root already exists on disk
    when the store is constructed. It must not repeat that confirmation on
    every subsequent write in the same store's lifetime, which is what keeps
    the common case cheap.
    """
    payload = b"a payload into an already-existing root"
    synced: list[Path] = []
    monkeypatch.setattr(blob_store, "_fsync_directory", lambda directory: synced.append(directory))
    store = FilesystemBlobStore(tmp_path)

    await store.put(chunks_of(payload))
    assert tmp_path.parent in synced, "a fresh instance must confirm root's parent entry once"
    synced.clear()

    await store.put(chunks_of(b"a different payload"))
    assert tmp_path.parent not in synced, "root durability must not be reconfirmed on every write"


async def test_a_failed_root_confirmation_is_retried_not_trusted_from_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash (or any exception) between ``mkdir`` and the confirming fsync
    leaves the root directory present on disk but unconfirmed. The bug this
    guards against: treating that on-disk presence as proof of durability
    would let a retry - in this process, or a fresh one after a restart -
    report success without ever successfully confirming the root's own
    directory entry, silently carrying the first attempt's failure forward.
    """
    root = tmp_path / "attachments"
    payload = b"the first attachment this store has ever seen"
    real_fsync_directory = blob_store._fsync_directory
    synced: list[Path] = []
    should_fail = [True]

    def flaky_fsync_directory(directory: Path) -> None:
        synced.append(directory)
        if should_fail[0]:
            should_fail[0] = False
            raise OSError("synthetic root-parent fsync failure")
        real_fsync_directory(directory)

    monkeypatch.setattr(blob_store, "_fsync_directory", flaky_fsync_directory)
    store = FilesystemBlobStore(root)

    with pytest.raises(OSError, match="synthetic root-parent fsync failure"):
        await store.put(chunks_of(payload))

    assert root.is_dir(), "mkdir already ran before the fsync failed - present but unconfirmed"
    assert store._root_confirmed_durable is False, (
        "a failed sync must never be recorded as confirmed"
    )

    synced.clear()
    stored = await store.put(chunks_of(payload))

    assert tmp_path in synced, (
        "the retry must redo the confirming fsync, not skip it because root exists"
    )
    assert store._root_confirmed_durable is True
    assert stored.deduplicated is False


async def test_a_restart_after_a_partial_multi_level_confirmation_resyncs_the_whole_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scenario the single-level retry test above does not cover: two new
    levels get created in one ``mkdir(parents=True)`` call, the *inner* one's
    confirmation succeeds, and only the *outer* one's - the deeper directory's
    entry inside its own parent - fails. Both directories are now present on
    disk; only a boolean this store instance holds knows the outer one was
    never actually confirmed. A fresh instance (standing in for a process
    restart, since a real crash can't be simulated in-process) starts with
    that boolean back at ``False`` - it must not see the outer directory
    merely ``.exists()`` and skip redoing its confirmation, or the crash this
    whole mechanism exists to survive would go undetected.
    """
    monkeypatch.chdir(tmp_path)
    root = Path("data") / "attachments"
    payload = b"a payload under a root nested two levels deep, confirmed after a restart"
    real_fsync_directory = blob_store._fsync_directory
    synced: list[Path] = []
    fail_next_cwd_sync = [True]

    def flaky_fsync_directory(directory: Path) -> None:
        synced.append(directory)
        # The outer level's confirmation is the fsync of *its* parent, cwd -
        # `data`'s own entry, one level short of the inner one already fixed.
        if directory == Path() and fail_next_cwd_sync[0]:
            fail_next_cwd_sync[0] = False
            raise OSError("synthetic failure confirming the outer level's entry in its parent")
        real_fsync_directory(directory)

    monkeypatch.setattr(blob_store, "_fsync_directory", flaky_fsync_directory)

    first_attempt = FilesystemBlobStore(root)
    with pytest.raises(OSError, match="synthetic failure confirming the outer level"):
        await first_attempt.put(chunks_of(payload))

    assert root.is_dir()
    assert Path("data").is_dir(), (
        "mkdir(parents=True) already created both levels before the sync failed"
    )

    # A fresh instance, not the one that failed - simulating a process
    # restart, where nothing in memory survives to remember what was and
    # wasn't confirmed.
    synced.clear()
    restarted = FilesystemBlobStore(root)
    stored = await restarted.put(chunks_of(payload))

    assert Path() in synced, "the restart must redo the outer level's confirmation, not skip it"
    assert restarted._root_confirmed_durable is True
    assert stored.deduplicated is False


async def test_a_deduplicated_write_still_confirms_directory_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry that lands on an already-written file must not skip its fsync.

    The first attempt can rename the file and then fail to fsync its
    directory entry - the file exists, but nothing confirmed the entry
    survives a crash, and that attempt's caller never got a StoredBlob back.
    A retry that finds the file already there and declares success without
    re-fsyncing would carry that unconfirmed state forward silently.
    """
    payload = b"payload written once, confirmed on every attempt"
    store = FilesystemBlobStore(tmp_path)
    calls: list[Path] = []
    real_fsync_directory = blob_store._fsync_directory

    def counting_fsync_directory(directory: Path) -> None:
        calls.append(directory)
        real_fsync_directory(directory)

    monkeypatch.setattr(blob_store, "_fsync_directory", counting_fsync_directory)

    await store.put(chunks_of(payload))
    assert calls, "the first write must confirm durability"
    calls.clear()

    second = await store.put(chunks_of(payload))

    assert second.deduplicated is True
    assert calls, "the dedup path must also confirm directory durability"


async def test_two_different_payloads_do_not_collide(tmp_path: Path) -> None:
    store = FilesystemBlobStore(tmp_path)

    first = await store.put(chunks_of(b"one"))
    second = await store.put(chunks_of(b"two"))

    assert first.storage_key != second.storage_key
    assert store.path_for(first.storage_key).read_bytes() == b"one"
    assert store.path_for(second.storage_key).read_bytes() == b"two"
