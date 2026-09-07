"""Khoj adapter port (docs/adr/0003, docs/DESIGN.md 8).

Scoped to what the index-sync, search-fusion, and Ask use cases need: upload/
replace content by filename, delete by filename, semantic query, and chat.
``chat`` (docs/DESIGN.md 7.6) resolves docs/adr/0003's "Contract spike
findings" 4 the way docs/adr/0010 decides: Khoj's own chat-model credentials
remain entirely the operator's choice (never configured by this project's own
code), and this project's own proxy is separately gated by
``Settings.ask_enabled`` (default off) before any question ever reaches
whichever model Khoj is configured with.
"""

from __future__ import annotations

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
    reference is mapped back to a ``document_id`` without depending on Khoj
    parsing YAML front matter.
    """

    compiled: str
    filename: str
    heading: str = ""


@dataclass(frozen=True, slots=True)
class KhojChatResult:
    response: str
    references: tuple[KhojChatReference, ...]


class KhojUnavailableError(Exception):
    """Khoj could not be reached, or returned an unexpected response.

    docs/DESIGN.md 7.5: hybrid search must degrade explicitly
    (``degraded=true``) rather than silently returning an empty success - a
    caller catches this and sets that flag, it is never swallowed here. Ask
    (docs/adr/0010) reuses this same exception for the identical reason.
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

    async def chat(self, question: str, *, limit: int = 5) -> KhojChatResult:
        """Ask over currently indexed content, in Khoj's ``/notes``-only mode
        (docs/DESIGN.md 7.6) - never online search or code execution, both of
        which are undeployed anyway (docs/adr/0003 decision 5).

        Raises ``KhojUnavailableError`` for the same reasons ``search`` does,
        *and* whenever Khoj has no chat model configured at all (docs/adr/0003
        finding 4) - from this port's caller's point of view, an
        unconfigured chat model and an unreachable Khoj both mean "no answer
        is available right now," and docs/DESIGN.md 7.5's degrade-explicit
        contract applies identically to both.
        """
        ...
