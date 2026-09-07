"""Khoj index sync (docs/DESIGN.md 7.2 steps 10-11, 8.3, docs/adr/0010).

A full resync of every current document revision - the simplest correct
implementation for this slice. Khoj's own upload endpoint replaces content by
filename (docs/adr/0003 contract-spike finding 1), so re-uploading a document
whose current revision has not changed since the last sync is a safe no-op,
not a bug to optimize away. An incremental, changed-only sync (a
``khoj_index_items`` tracking table, docs/DESIGN.md 6.5) and triggering this
automatically at the end of organize (docs/DESIGN.md 7.2 steps 10-11) are
documented follow-ups, not built by this slice - today, syncing is always an
explicit, owner-triggered action (``POST /v1/admin/khoj-sync``).
"""

from __future__ import annotations

from dataclasses import dataclass

from tc_domain.capture import WorkspaceId
from tc_domain.khoj_export import DocumentExportSource, khoj_filename, khoj_markdown
from tc_domain.khoj_ports import KhojIndexFile, KhojPort


@dataclass(frozen=True, slots=True)
class KhojSyncResult:
    documents_indexed: int


class SyncKhojIndex:
    def __init__(self, documents: DocumentExportSource, khoj: KhojPort) -> None:
        self._documents = documents
        self._khoj = khoj

    async def __call__(self, workspace_id: WorkspaceId) -> KhojSyncResult:
        records = await self._documents.list_all_current_for_export(workspace_id)
        files = tuple(
            KhojIndexFile(
                filename=khoj_filename(
                    workspace_id=workspace_id,
                    kind=record.kind,
                    stable_key=record.stable_key,
                    document_id=record.id,
                ),
                content=khoj_markdown(
                    document_id=record.id,
                    revision_id=record.revision_id,
                    workspace_id=workspace_id,
                    kind=record.kind,
                    stable_key=record.stable_key,
                    title=record.title,
                    body_markdown=record.body_markdown,
                    window_start=record.window_start,
                    window_end=record.window_end,
                    entities=record.entities,
                    source_thought_ids=record.source_thought_ids,
                ),
            )
            for record in records
        )
        # A no-op tuple is handled by ``KhojPort.index`` itself (an empty
        # workspace has nothing to sync); no special-casing needed here.
        await self._khoj.index(files)
        return KhojSyncResult(documents_indexed=len(files))
