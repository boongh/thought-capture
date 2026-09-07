"""Khoj adapter port (docs/adr/0003, docs/DESIGN.md 8).

Scoped to what the index-sync, search-fusion, and Ask use cases need: upload/
replace content by filename, delete by filename, semantic query, and chat.
``chat`` (docs/DESIGN.md 7.6) resolves docs/adr/0003's "Contract spike
findings" 4 the way this ADR's "Ask proxy" amendment decides: Khoj's own
chat-model credentials remain entirely the operator's choice (never
configured by this project's own code), and this project's own proxy is
separately gated by ``Settings.ask_enabled`` (default off) before any
question ever reaches whichever model Khoj is configured with.

``chat`` streams (docs/DESIGN.md 7.6: "The gateway streams the answer"), one
``KhojChatChunk`` per increment - review feedback on the first cut of this
port (a single buffered ``chat()`` coroutine) correctly flagged that as a
deviation from the accepted contract, not a stylistic choice. Streaming is
also what makes the "Ask proxy" amendment's conversation-retention fix
possible at all: only the streamed response carries a ``METADATA`` event with
Khoj's own ``conversationId``, which ``HttpKhojClient`` needs to delete the
conversation immediately after use (see that adapter's own docstring) - the
non-streaming response never surfaces it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class KhojIndexFile:
    """One file to upload/replace in Khoj's index.

    ``filename`` must follow docs/DESIGN.md 8.3's convention
    (``{workspace_id}/{kind}/{stable_key_slug}--{document_id}.md``) - Khoj
    echoes it back verbatim in a search result's ``additional.file``
    (docs/adr/0003, contract-spike finding 2), which is how a result is
    mapped back to a document without depending on Khoj parsing YAML front
    matter itself.
    """

    filename: str
    content: bytes


@dataclass(frozen=True, slots=True)
class KhojSearchResult:
    entry: str
    score: float
    filename: str


@dataclass(frozen=True, slots=True)
class KhojChatReference:
    """One retrieved note Khoj's answer was grounded in.

    ``filename`` is the same ``additional.file``/``file`` convention as
    ``KhojSearchResult.filename`` (docs/DESIGN.md 8.3), which is how a
    reference is mapped back to a document without depending on Khoj parsing
    YAML front matter.
    """

    compiled: str
    filename: str
    heading: str = ""


@dataclass(frozen=True, slots=True)
class KhojChatChunk:
    """One increment of a streaming Ask answer.

    Exactly one of the three optional fields is set on any given chunk - a
    caller accumulates ``text_delta`` for the full answer text, captures
    ``references`` when it arrives (sent once, before any text), and stops
    iterating once a chunk with ``done=True`` is seen. Empty defaults mean a
    consumer that only cares about final text can safely concatenate every
    ``text_delta`` without checking which chunk carried it.
    """

    text_delta: str = ""
    references: tuple[KhojChatReference, ...] | None = None
    done: bool = False


class KhojUnavailableError(Exception):
    """Khoj could not be reached, or returned an unexpected response.

    docs/DESIGN.md 7.5: hybrid search must degrade explicitly
    (``degraded=true``) rather than silently returning an empty success - a
    caller catches this and sets that flag, it is never swallowed here. Ask
    reuses this same exception for the identical reason.
    """


@runtime_checkable
class KhojPort(Protocol):
    async def index(self, files: tuple[KhojIndexFile, ...]) -> None:
        """Upload/replace content by filename. A no-op for an empty tuple."""
        ...

    async def delete(self, filenames: tuple[str, ...]) -> None:
        """Delete by filename. A no-op for an empty tuple or an unknown name."""
        ...

    async def search(self, q: str, *, limit: int = 5) -> tuple[KhojSearchResult, ...]:
        """Semantic search over currently indexed content."""
        ...

    def chat(self, question: str, *, limit: int = 5) -> AsyncIterator[KhojChatChunk]:
        """Ask over currently indexed content, in Khoj's ``/notes``-only mode
        (docs/DESIGN.md 7.6) - never online search or code execution, both of
        which are undeployed anyway (docs/adr/0003 decision 5). Streams the
        answer (docs/DESIGN.md 7.6) rather than buffering it.

        Raises ``KhojUnavailableError`` for the same reasons ``search`` does,
        *and* whenever Khoj has no chat model configured at all (docs/adr/0003
        finding 4) - from this port's caller's point of view, an
        unconfigured chat model and an unreachable Khoj both mean "no answer
        is available right now," and docs/DESIGN.md 7.5's degrade-explicit
        contract applies identically to both. An implementation that has
        already yielded a partial answer and then fails mid-stream still
        raises rather than silently ending the iterator, so a caller cannot
        mistake a cut-off answer for a complete one.
        """
        ...
