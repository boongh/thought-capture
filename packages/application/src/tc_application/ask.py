"""Ask use case (docs/DESIGN.md 7.6, docs/adr/0010).

Delegates to Khoj's ``/notes``-mode chat. Gated by ``enabled``, resolved once
at composition time from ``Settings.ask_enabled`` and never read from
settings directly here - the same reason ``OrganizeWindow`` receives an
already-built provider instead of reading ``Settings`` itself (docs/DESIGN.md
5.3). ``enabled=False`` is docs/adr/0010's default: Ask must be explicitly
turned on *in addition to* Khoj itself having a chat model configured
(docs/adr/0003 finding 4) before any question reaches whatever model Khoj is
configured with.
"""

from __future__ import annotations

import uuid

from tc_domain.ask import AskAnswer, AskReference
from tc_domain.capture import WorkspaceId
from tc_domain.khoj_export import parse_khoj_filename
from tc_domain.khoj_ports import KhojChatReference, KhojPort, KhojUnavailableError
from tc_domain.search import ExactSearchPort, SearchQuery

# docs/DESIGN.md 7.6 asks Khoj for a handful of grounding notes, not a full
# search page - matches ``KhojPort.chat``'s own default.
_REFERENCE_LIMIT = 5


class AskQuestion:
    def __init__(self, khoj: KhojPort, exact: ExactSearchPort, *, enabled: bool) -> None:
        self._khoj = khoj
        self._exact = exact
        self._enabled = enabled

    async def __call__(self, workspace_id: WorkspaceId, question: str) -> AskAnswer:
        if not self._enabled:
            return AskAnswer(enabled=False, degraded=False, answer=None, references=())

        try:
            result = await self._khoj.chat(question, limit=_REFERENCE_LIMIT)
        except KhojUnavailableError:
            return AskAnswer(enabled=True, degraded=True, answer=None, references=())

        references = await self._resolve_references(workspace_id, result.references, question)
        return AskAnswer(
            enabled=True, degraded=False, answer=result.response, references=references
        )

    async def _resolve_references(
        self,
        workspace_id: WorkspaceId,
        chat_references: tuple[KhojChatReference, ...],
        question: str,
    ) -> tuple[AskReference, ...]:
        """Map each Khoj reference's filename back to a citable document, the
        same workspace-scoped-verified-and-deduped way ``Search._semantic``
        does (docs/adr/0010) - Khoj can chunk one uploaded document into
        several indexed entries and cite more than one in the same answer, so
        this must not return the same ``AskReference`` twice.
        """
        seen: set[uuid.UUID] = set()
        matched: list[tuple[uuid.UUID, KhojChatReference]] = []
        for ref in chat_references:
            parts = parse_khoj_filename(ref.filename)
            if parts is None or parts.workspace_id != workspace_id:
                continue
            if parts.document_id in seen:
                continue
            seen.add(parts.document_id)
            matched.append((parts.document_id, ref))
        if not matched:
            return ()

        document_ids = tuple(document_id for document_id, _ in matched)
        hydrated = await self._exact.hydrate(workspace_id, document_ids, SearchQuery(q=question))

        resolved: list[AskReference] = []
        for document_id, _ref in matched:
            base = hydrated.get(document_id)
            if base is None:
                # Khoj cited a file no longer the current revision in
                # PostgreSQL - dropped, not surfaced as a dangling citation
                # (docs/DESIGN.md 8.2).
                continue
            resolved.append(
                AskReference(document_id=document_id, title=base.title, snippet=base.snippet)
            )
        return tuple(resolved)
