"""Khoj Markdown export format and filename round-trip (docs/DESIGN.md 8.3)."""

from __future__ import annotations

import datetime as dt
import uuid

from hypothesis import given
from hypothesis import strategies as st

from tc_domain.khoj_export import (
    DocumentExport,
    khoj_filename,
    parse_khoj_filename,
    render_document_markdown,
    slugify,
)

WORKSPACE = uuid.uuid4()
DOCUMENT = uuid.uuid4()
REVISION = uuid.uuid4()


def _export(**overrides: object) -> DocumentExport:
    defaults: dict[str, object] = {
        "document_id": DOCUMENT,
        "revision_id": REVISION,
        "workspace_id": WORKSPACE,
        "kind": "project",
        "stable_key": "project:thought-capture-ai",
        "title": "Thought Capture AI",
        "body_markdown": "## Summary\n\nsomething",
        "window_start": dt.datetime(2026, 8, 29, 13, tzinfo=dt.UTC),
        "window_end": dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC),
        "entities": ("Thought Capture AI", "Khoj"),
        "source_thought_ids": (101, 105),
    }
    defaults.update(overrides)
    return DocumentExport(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Filename build/parse
# ---------------------------------------------------------------------------


def test_filename_matches_the_documented_convention() -> None:
    name = khoj_filename(
        workspace_id=WORKSPACE,
        kind="project",
        stable_key="project:thought-capture-ai",
        document_id=DOCUMENT,
    )
    assert name == f"{WORKSPACE}/project/project-thought-capture-ai--{DOCUMENT}.md"


def test_parse_recovers_workspace_and_document_id() -> None:
    name = khoj_filename(
        workspace_id=WORKSPACE,
        kind="person",
        stable_key="person:jane-rivera",
        document_id=DOCUMENT,
    )

    parsed = parse_khoj_filename(name)

    assert parsed is not None
    assert parsed.workspace_id == WORKSPACE
    assert parsed.document_id == DOCUMENT


def test_parse_returns_none_for_an_unrecognized_shape() -> None:
    assert parse_khoj_filename("not-a-khoj-filename.md") is None
    assert parse_khoj_filename(f"{WORKSPACE}/project/missing-document-id.md") is None
    assert parse_khoj_filename("") is None


_KINDS = [
    "daily_digest",
    "person",
    "project",
    "place",
    "organization",
    "topic",
    "decision",
    "todo",
]


@given(stable_key=st.text(min_size=1, max_size=80), kind=st.sampled_from(_KINDS))
def test_filename_round_trip_always_recovers_the_same_document_id(
    stable_key: str, kind: str
) -> None:
    name = khoj_filename(
        workspace_id=WORKSPACE, kind=kind, stable_key=stable_key, document_id=DOCUMENT
    )

    parsed = parse_khoj_filename(name)

    assert parsed is not None
    assert parsed.document_id == DOCUMENT
    assert parsed.workspace_id == WORKSPACE


def test_slugify_is_lowercase_and_filename_safe() -> None:
    assert slugify("Project: Thought Capture AI!") == "project-thought-capture-ai"


def test_slugify_never_returns_empty() -> None:
    assert slugify("") == "untitled"
    assert slugify("###") == "untitled"


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def test_render_includes_the_documented_front_matter_fields() -> None:
    body = render_document_markdown(_export()).decode("utf-8")

    assert body.startswith("---\n")
    assert f'document_id: "{DOCUMENT}"' in body
    assert f'revision_id: "{REVISION}"' in body
    assert f'workspace_id: "{WORKSPACE}"' in body
    assert 'kind: "project"' in body
    assert 'stable_key: "project:thought-capture-ai"' in body
    assert 'window_start: "2026-08-29T13:00:00+00:00"' in body
    assert 'window_end: "2026-08-30T13:00:00+00:00"' in body
    assert 'entities: ["Thought Capture AI", "Khoj"]' in body
    assert "source_thought_ids: [101, 105]" in body
    assert "# Thought Capture AI" in body
    assert "## Summary\n\nsomething" in body


def test_render_omits_window_fields_when_absent() -> None:
    body = render_document_markdown(_export(window_start=None, window_end=None)).decode("utf-8")

    assert "window_start:" not in body
    assert "window_end:" not in body


def test_render_escapes_quotes_in_the_title() -> None:
    body = render_document_markdown(_export(title='The "Aurora" project')).decode("utf-8")
    assert '# The "Aurora" project' in body
