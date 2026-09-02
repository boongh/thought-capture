"""Khoj adapter port (docs/adr/0003, docs/DESIGN.md 8).

Scoped to what the index-sync and search-fusion use cases need: upload/
replace content by filename, delete by filename, and query. Ask (docs/DESIGN.md
7.6) is a separate, not-yet-implemented concern - see docs/adr/0003's
"Contract spike findings" 4 for why it needs its own decision before any code
depends on it.
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


class KhojUnavailableError(Exception):
    """Khoj could not be reached, or returned an unexpected response.

    docs/DESIGN.md 7.5: hybrid search must degrade explicitly
    (``degraded=true``) rather than silently returning an empty success - a
    caller catches this and sets that flag, it is never swallowed here.
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
