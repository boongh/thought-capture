"""Ask use case (docs/DESIGN.md 7.6, docs/adr/0003's "Ask proxy" amendment).

Delegates to Khoj's ``/notes``-mode chat, streaming (docs/DESIGN.md 7.6: "The
gateway streams the answer"). Gated by ``enabled``, resolved once at
composition time from ``Settings.ask_enabled`` (itself derived from two
independent opt-ins, see ``Settings``) and never read from settings directly
here - the same reason ``OrganizeWindow`` receives an already-built provider
instead of reading ``Settings`` itself (docs/DESIGN.md 5.3).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from tc_domain.ask import AskAnswer, AskChunk, AskReference
from tc_domain.capture import WorkspaceId
from tc_domain.khoj_ports import KhojChatReference, KhojPort, KhojUnavailableError
from tc_domain.search import ExactSearchPort, SearchQuery, SemanticHydrator, has_strict_filters

# docs/DESIGN.md 7.6 asks Khoj for a handful of grounding notes, not a full
# search page - matches ``KhojPort.chat``'s own default.
_REFERENCE_LIMIT = 5


class AskQuestion:
    def __init__(
        self,
        khoj: KhojPort,
        hydrate: SemanticHydrator,
        exact: ExactSearchPort,
        *,
        enabled: bool,
    ) -> None:
        self._khoj = khoj
        self._hydrate = hydrate
        self._exact = exact
        self._enabled = enabled

    async def __call__(
        self, workspace_id: WorkspaceId, query: SearchQuery
    ) -> AsyncIterator[AskChunk]:
        if not self._enabled:
            yield AskChunk(enabled=False, degraded=False, strict_unsupported=False, done=True)
            return

        if has_strict_filters(query):
            # docs/DESIGN.md 7.6: "strict Ask returns a clear capability code
            # and offers exact search results; it does not silently answer
            # from an unconstrained corpus." No Khoj/LLM call is made at all -
            # Khoj's evidence-injection path for honoring these filters was
            # never verified (docs/adr/0003 "Contract spike findings" item 3).
            fallback = await self._exact.search(workspace_id, query)
            yield AskChunk(
                enabled=True,
                degraded=False,
                strict_unsupported=True,
                fallback=fallback,
                done=True,
            )
            return

        try:
            async for piece in self._khoj.chat(query.q or "", limit=_REFERENCE_LIMIT):
                if piece.text_delta:
                    yield AskChunk(
                        enabled=True,
                        degraded=False,
                        strict_unsupported=False,
                        text_delta=piece.text_delta,
                    )
                if piece.references is not None:
                    resolved = await self._resolve_references(workspace_id, piece.references)
                    yield AskChunk(
                        enabled=True,
                        degraded=False,
                        strict_unsupported=False,
                        references=resolved,
                    )
                if piece.done:
                    yield AskChunk(
                        enabled=True, degraded=False, strict_unsupported=False, done=True
                    )
        except KhojUnavailableError:
            # A partial answer may already have been yielded above (a chunk
            # part-way through streaming, then Khoj drops) - the caller keeps
            # whatever text/references it already has, plus this explicit
            # signal that the answer is incomplete, never a silent cutoff
            # mistaken for a complete one (docs/DESIGN.md 7.5).
            yield AskChunk(enabled=True, degraded=True, strict_unsupported=False, done=True)

    async def _resolve_references(
        self, workspace_id: WorkspaceId, chat_references: tuple[KhojChatReference, ...]
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
        filenames = tuple(dict.fromkeys(ref.filename for ref in chat_references))
        if not filenames:
            return ()
        hydrated = await self._hydrate.hydrate(workspace_id, SearchQuery(), filenames)

        seen: set[str] = set()
        resolved: list[AskReference] = []
        for ref in chat_references:
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


async def collect_ask_answer(chunks: AsyncIterator[AskChunk]) -> AskAnswer:
    """Drains an ``AskChunk`` stream into one ``AskAnswer`` - for a caller
    (Discord's ``/ask``) with no use for real token-level streaming.
    docs/DESIGN.md 7.6's "the gateway streams the answer" is about the HTTP
    API surface; a Discord slash-command reply is not well suited to
    token-by-token message edits, so it drains the same use case's stream
    internally instead of exposing a second, non-streaming code path.
    """
    enabled = False
    degraded = False
    strict_unsupported = False
    text_parts: list[str] = []
    references: tuple[AskReference, ...] = ()
    fallback = None
    async for chunk in chunks:
        enabled = chunk.enabled
        degraded = degraded or chunk.degraded
        strict_unsupported = strict_unsupported or chunk.strict_unsupported
        if chunk.text_delta:
            text_parts.append(chunk.text_delta)
        if chunk.references is not None:
            references = chunk.references
        if chunk.fallback is not None:
            fallback = chunk.fallback

    return AskAnswer(
        enabled=enabled,
        degraded=degraded,
        strict_unsupported=strict_unsupported,
        answer="".join(text_parts) if text_parts else None,
        references=references,
        fallback=fallback,
    )
