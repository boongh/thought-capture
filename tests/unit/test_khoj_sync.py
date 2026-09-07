"""``SyncKhojIndex`` (docs/DESIGN.md 7.2 steps 10-11, docs/adr/0010)."""

from __future__ import annotations

import uuid

import pytest

from tc_application.khoj_sync import SyncKhojIndex
from tc_domain.capture import WorkspaceId
from tc_domain.khoj_export import (
    DocumentExportRecord,
    khoj_filename,
    khoj_markdown,
    parse_khoj_filename,
)
from tc_domain.khoj_ports import KhojUnavailableError
from tests.unit.fakes import FakeDocumentExportSource, FakeKhoj

WORKSPACE = WorkspaceId(uuid.uuid4())


def _record(**overrides: object) -> DocumentExportRecord:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "kind": "project",
        "stable_key": "project:x",
        "title": "X",
        "revision_id": uuid.uuid4(),
        "body_markdown": "body",
        "window_start": None,
        "window_end": None,
        "entities": (),
        "source_thought_ids": (),
    }
    defaults.update(overrides)
    return DocumentExportRecord(**defaults)  # type: ignore[arg-type]


async def test_syncs_every_current_document_as_one_index_call() -> None:
    record_a = _record()
    record_b = _record()
    documents = FakeDocumentExportSource([record_a, record_b])
    khoj = FakeKhoj()
    sync = SyncKhojIndex(documents, khoj)

    result = await sync(WORKSPACE)

    assert result.documents_indexed == 2
    assert documents.calls == [WORKSPACE]
    assert len(khoj.indexed) == 1  # one batched call, not one per document
    (files,) = khoj.indexed
    assert len(files) == 2
    filenames = {f.filename for f in files}
    assert filenames == {
        khoj_filename(
            workspace_id=WORKSPACE,
            kind=record_a.kind,
            stable_key=record_a.stable_key,
            document_id=record_a.id,
        ),
        khoj_filename(
            workspace_id=WORKSPACE,
            kind=record_b.kind,
            stable_key=record_b.stable_key,
            document_id=record_b.id,
        ),
    }


async def test_uploaded_content_matches_the_shared_khoj_export_format() -> None:
    record = _record()
    documents = FakeDocumentExportSource([record])
    khoj = FakeKhoj()
    sync = SyncKhojIndex(documents, khoj)

    await sync(WORKSPACE)

    (files,) = khoj.indexed
    uploaded = files[0]
    expected_content = khoj_markdown(
        document_id=record.id,
        revision_id=record.revision_id,
        workspace_id=WORKSPACE,
        kind=record.kind,
        stable_key=record.stable_key,
        title=record.title,
        body_markdown=record.body_markdown,
        window_start=record.window_start,
        window_end=record.window_end,
        entities=record.entities,
        source_thought_ids=record.source_thought_ids,
    )
    assert uploaded.content == expected_content
    parsed = parse_khoj_filename(uploaded.filename)
    assert parsed is not None
    assert parsed.document_id == record.id


async def test_an_empty_workspace_is_a_no_op() -> None:
    documents = FakeDocumentExportSource([])
    khoj = FakeKhoj()
    sync = SyncKhojIndex(documents, khoj)

    result = await sync(WORKSPACE)

    assert result.documents_indexed == 0
    assert khoj.indexed == [()]


async def test_propagates_khoj_unavailable() -> None:
    documents = FakeDocumentExportSource([_record()])
    khoj = FakeKhoj(raises=KhojUnavailableError("down"))
    sync = SyncKhojIndex(documents, khoj)

    with pytest.raises(KhojUnavailableError):
        await sync(WORKSPACE)
