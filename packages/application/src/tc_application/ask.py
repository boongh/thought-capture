"""Ask use case (docs/DESIGN.md 7.6, docs/adr/0003's "Ask proxy" amendment).

Delegates to Khoj's ``/notes``-mode chat. Gated by ``enabled``, resolved once
at composition time from ``Settings.ask_enabled`` and never read from
settings directly here - the same reason ``OrganizeWindow`` receives an
already-built provider instead of reading ``Settings`` itself (docs/DESIGN.md
5.3). ``enabled=False`` is the default: Ask must be explicitly turned on *in
addition to* Khoj itself having a chat model configured (docs/adr/0003
finding 4) before any question reaches whatever model Khoj is configured
with.
"""

from __future__ import annotations

from tc_domain.ask import AskAnswer, AskReference
from tc_domain.capture import WorkspaceId
from tc_domain.khoj_ports import KhojChatResult, KhojPort, KhojUnavailableError
from tc_domain.search import SearchQuery, SemanticHydrator

# docs/DESIGN.md 7.6 asks Khoj for a handful of grounding notes, not a full
# search page - matches ``KhojPort.chat``'s own default.
_REFERENCE_LIMIT = 5


class AskQuestion:
    def __init__(self, khoj: KhojPort, hydrate: SemanticHydrator, *, enabled: bool) -> None:
        self._khoj = khoj
        self._hydrate = hydrate
        self._enabled = enabled

    async def __call__(self, workspace_id: WorkspaceId, question: str) -> AskAnswer:
        if not self._enabled:
            return AskAnswer(enabled=False, degraded=False, answer=None, references=())

        try:
            result = await self._khoj.chat(question, limit=_REFERENCE_LIMIT)
        except KhojUnavailableError:
            return AskAnswer(enabled=True, degraded=True, answer=None, references=())

        references = await self._resolve_references(workspace_id, result)
        return AskAnswer(
            enabled=True, degraded=False, answer=result.response, references=references
        )

    async def _resolve_references(
        self, workspace_id: WorkspaceId, result: KhojChatResult
    ) -> tuple[AskReference, ...]:
        """Map each Khoj reference's filename back to a citable document via
        ``SemanticHydrator`` - the same workspace-scoped, filter-re-checked,
        current-revision-only resolution ``Search._semantic_results`` uses
        for search hits (``PostgresSemanticHydrator``'s own contract).

        Deduped by filename before hydrating and again while building the
        result: Khoj can chunk one uploaded document into more than one
        indexed entry and cite more than one chunk of it in the same answer
        (its own filename is identical across chunks of the same upload),
        which would otherwise return the same ``AskReference`` twice.
        """
        filenames = tuple(dict.fromkeys(ref.filename for ref in result.references))
        if not filenames:
            return ()
        hydrated = await self._hydrate.hydrate(workspace_id, SearchQuery(), filenames)

        seen: set[str] = set()
        resolved: list[AskReference] = []
        for ref in result.references:
            if ref.filename in seen:
                continue
            match = hydrated.get(ref.filename)
            if match is None:
                # Khoj cited a file that did not resolve - unparseable,
                # another workspace's, filtered out, or no longer Khoj's own
                # acknowledged current revision (docs/DESIGN.md 8.2) - dropped
                # rather than surfaced as a dangling citation.
                continue
            seen.add(ref.filename)
            resolved.append(
                AskReference(
                    document_id=match.document_id, title=match.title, snippet=match.snippet
                )
            )
        return tuple(resolved)
