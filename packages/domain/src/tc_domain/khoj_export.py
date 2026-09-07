"""Khoj Markdown export format (docs/DESIGN.md 8.3, docs/adr/0010).

Pure and stdlib-only, like the rest of the domain (docs/DESIGN.md 5.3): the
actual HTTP upload is ``tc_infrastructure``'s job. Front matter values are
rendered with ``json.dumps`` rather than a hand-rolled YAML escaper - a JSON
string/array literal is already a valid YAML flow scalar/sequence, so this
gets correct quoting (titles or entity names containing ``"``, ``:``, or
non-ASCII characters) for free, without adding a YAML dependency this layer
does not otherwise need.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tc_domain.capture import WorkspaceId

_NON_SLUG = re.compile(r"[^a-z0-9]+")

# One more than the fixed length of ``str(uuid.UUID(...))`` (32 hex digits +
# 4 hyphens = 36), used to split a filename stem back into its slug and
# document-id halves regardless of what characters the slug itself contains.
_UUID_STR_LENGTH = 36
_DOCUMENT_ID_SEPARATOR = "--"


def slugify_stable_key(stable_key: str) -> str:
    """Fold a ``documents.stable_key`` into the filename-safe form docs/DESIGN.md
    8.3 calls ``stable_key_slug``.

    Most ``stable_key`` values (entity documents, docs/DESIGN.md 6.3) are
    already filename-safe (``tc_domain.entities.document_stable_key``
    guarantees ASCII letters/digits/hyphens/a single ``:``). A daily digest's
    ``stable_key`` is an ISO 8601 timestamp instead (the capture-window end),
    which contains ``:`` and a UTC-offset ``+`` - both unsafe in a filename on
    at least one deployment target (docs/DESIGN.md 13's Windows/WSL2
    development target) - so this lower-cases and replaces every run of
    non-alphanumeric characters with a single hyphen, the same fold
    ``tc_domain.entities.document_stable_key`` already applies to entity
    names, rather than assuming the input is already safe.
    """
    slug = _NON_SLUG.sub("-", stable_key.lower()).strip("-")
    return slug or "untitled"


def khoj_filename(
    *, workspace_id: uuid.UUID, kind: str, stable_key: str, document_id: uuid.UUID
) -> str:
    """``{workspace_id}/{kind}/{stable_key_slug}--{document_id}.md`` (docs/DESIGN.md 8.3)."""
    slug = slugify_stable_key(stable_key)
    return f"{workspace_id}/{kind}/{slug}{_DOCUMENT_ID_SEPARATOR}{document_id}.md"


@dataclass(frozen=True, slots=True)
class KhojFilenameParts:
    """What a search/chat result's echoed filename recovers, without trusting
    ``entry``/``compiled`` content or Khoj's own YAML parsing (docs/adr/0003
    contract-spike finding 2)."""

    workspace_id: uuid.UUID
    kind: str
    document_id: uuid.UUID


def parse_khoj_filename(filename: str) -> KhojFilenameParts | None:
    """Inverse of ``khoj_filename``. ``None`` for anything not shaped like a
    file this project itself uploaded - Khoj's index is scoped to only our
    own exports (docs/domain/khoj_ports.py's ``CONTENT_TYPE`` restriction on
    the client side), but a caller must not trust that as a security boundary
    on the read side too.
    """
    parts = filename.split("/")
    if len(parts) != 3:
        return None
    workspace_part, kind, tail = parts
    if not tail.endswith(".md"):
        return None
    stem = tail[: -len(".md")]
    separator_start = len(stem) - _UUID_STR_LENGTH - len(_DOCUMENT_ID_SEPARATOR)
    if separator_start < 0:
        return None
    if (
        stem[separator_start : separator_start + len(_DOCUMENT_ID_SEPARATOR)]
        != _DOCUMENT_ID_SEPARATOR
    ):
        return None
    document_id_part = stem[separator_start + len(_DOCUMENT_ID_SEPARATOR) :]
    try:
        workspace_id = uuid.UUID(workspace_part)
        document_id = uuid.UUID(document_id_part)
    except ValueError:
        return None
    return KhojFilenameParts(workspace_id=workspace_id, kind=kind, document_id=document_id)


def khoj_markdown(
    *,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    workspace_id: uuid.UUID,
    kind: str,
    stable_key: str,
    title: str,
    body_markdown: str,
    window_start: dt.datetime | None,
    window_end: dt.datetime | None,
    entities: tuple[str, ...],
    source_thought_ids: tuple[int, ...],
) -> bytes:
    """Render one document revision as the Markdown docs/DESIGN.md 8.3 specifies.

    ``window_start``/``window_end`` come from the *writing* run
    (``document_revisions.run_id`` -> ``runs``), not necessarily the window
    the document was first created in - which is the correct provenance for
    "when was this content last confirmed accurate", the same reasoning
    ``updated_at`` elsewhere in this codebase already uses.
    """
    front_matter = {
        "document_id": str(document_id),
        "revision_id": str(revision_id),
        "workspace_id": str(workspace_id),
        "kind": kind,
        "stable_key": stable_key,
        "window_start": window_start.isoformat() if window_start is not None else None,
        "window_end": window_end.isoformat() if window_end is not None else None,
        "entities": list(entities),
        "source_thought_ids": list(source_thought_ids),
    }
    lines = ["---"]
    for key, value in front_matter.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(body_markdown)
    return "\n".join(lines).encode("utf-8")


@dataclass(frozen=True, slots=True)
class DocumentExportRecord:
    """One current document revision, everything ``khoj_markdown`` needs to
    render it (docs/adr/0010). Defined once here, not duplicated per layer:
    ``tc_infrastructure``'s reader fills it in from PostgreSQL and
    ``tc_application``'s sync use case consumes it structurally unchanged."""

    id: uuid.UUID
    kind: str
    stable_key: str
    title: str
    revision_id: uuid.UUID
    body_markdown: str
    window_start: dt.datetime | None
    window_end: dt.datetime | None
    entities: tuple[str, ...]
    source_thought_ids: tuple[int, ...]


@runtime_checkable
class DocumentExportSource(Protocol):
    async def list_all_current_for_export(
        self, workspace_id: WorkspaceId
    ) -> list[DocumentExportRecord]:
        """Every current document revision in the workspace, unpaginated
        (docs/adr/0010's full-resync Khoj sync)."""
        ...
