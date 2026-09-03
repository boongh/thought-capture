"""Markdown export format for Khoj indexing (docs/DESIGN.md 8.3, docs/adr/0003).

Pure formatting/parsing - no framework imports, no I/O (docs/DESIGN.md 5.3).
``khoj_filename``/``parse_khoj_filename`` are inverses of each other by
construction: a search result only ever carries a filename this module itself
built, so a round trip must always recover the same ``document_id`` (proven by
a property test, not just example tests).
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass

_SLUG_DISALLOWED = re.compile(r"[^a-z0-9._-]+")


def slugify(stable_key: str) -> str:
    """Lowercase, filename-safe. Not guaranteed reversible - only ``document_id``
    round-trips through a filename (see ``parse_khoj_filename``); the slug is a
    human-readable hint, not addressable state."""
    slug = _SLUG_DISALLOWED.sub("-", stable_key.lower()).strip("-")
    return slug or "untitled"


def khoj_filename(
    *, workspace_id: uuid.UUID, kind: str, stable_key: str, document_id: uuid.UUID
) -> str:
    """``{workspace_id}/{kind}/{stable_key_slug}--{document_id}.md`` (docs/DESIGN.md 8.3)."""
    return f"{workspace_id}/{kind}/{slugify(stable_key)}--{document_id}.md"


@dataclass(frozen=True, slots=True)
class KhojFilenameParts:
    workspace_id: uuid.UUID
    document_id: uuid.UUID


_FILENAME_PATTERN = re.compile(
    r"^(?P<workspace_id>[0-9a-fA-F-]{36})/[^/]+/[^/]*--(?P<document_id>[0-9a-fA-F-]{36})\.md$"
)


def parse_khoj_filename(filename: str) -> KhojFilenameParts | None:
    """Recover ``(workspace_id, document_id)`` from a Khoj search result's
    ``additional.file`` (docs/adr/0003 contract finding 2). ``None`` for any
    shape this module did not itself produce - a caller must skip an
    unparseable result, never raise, so one malformed or foreign entry cannot
    fail an entire search request.
    """
    match = _FILENAME_PATTERN.match(filename)
    if match is None:
        return None
    try:
        return KhojFilenameParts(
            workspace_id=uuid.UUID(match.group("workspace_id")),
            document_id=uuid.UUID(match.group("document_id")),
        )
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class DocumentExport:
    """Everything ``render_document_markdown`` needs for one document's current revision."""

    document_id: uuid.UUID
    revision_id: uuid.UUID
    workspace_id: uuid.UUID
    kind: str
    stable_key: str
    title: str
    body_markdown: str
    window_start: dt.datetime | None
    window_end: dt.datetime | None
    entities: tuple[str, ...]
    source_thought_ids: tuple[int, ...]

    @property
    def filename(self) -> str:
        return khoj_filename(
            workspace_id=self.workspace_id,
            kind=self.kind,
            stable_key=self.stable_key,
            document_id=self.document_id,
        )


def _yaml_string(value: str) -> str:
    """Double-quoted YAML scalar. Front-matter values here are ids/titles, not
    arbitrary prose, so a fixed quote-and-escape is sufficient - no block
    scalars or multiline handling needed."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_string_list(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_yaml_string(v) for v in values) + "]"


def _yaml_int_list(values: tuple[int, ...]) -> str:
    return "[" + ", ".join(str(v) for v in values) + "]"


def render_document_markdown(export: DocumentExport) -> bytes:
    """Front matter + body, per the docs/DESIGN.md 8.3 example exactly."""
    lines = [
        "---",
        f"document_id: {_yaml_string(str(export.document_id))}",
        f"revision_id: {_yaml_string(str(export.revision_id))}",
        f"workspace_id: {_yaml_string(str(export.workspace_id))}",
        f"kind: {_yaml_string(export.kind)}",
        f"stable_key: {_yaml_string(export.stable_key)}",
    ]
    if export.window_start is not None:
        lines.append(f"window_start: {_yaml_string(export.window_start.isoformat())}")
    if export.window_end is not None:
        lines.append(f"window_end: {_yaml_string(export.window_end.isoformat())}")
    lines.append(f"entities: {_yaml_string_list(export.entities)}")
    lines.append(f"source_thought_ids: {_yaml_int_list(export.source_thought_ids)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {export.title}")
    lines.append("")
    lines.append(export.body_markdown)
    return "\n".join(lines).encode("utf-8")
