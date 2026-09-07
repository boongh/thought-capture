"""Khoj Markdown export format (docs/DESIGN.md 8.3, docs/adr/0010)."""

from __future__ import annotations

import datetime as dt
import uuid

import yaml

from tc_domain.khoj_export import khoj_filename, khoj_markdown, parse_khoj_filename

WORKSPACE = uuid.uuid4()
DOCUMENT = uuid.uuid4()


def test_filename_round_trips_through_parse() -> None:
    filename = khoj_filename(
        workspace_id=WORKSPACE,
        kind="project",
        stable_key="project:thought-capture-ai",
        document_id=DOCUMENT,
    )

    parsed = parse_khoj_filename(filename)

    assert parsed is not None
    assert parsed.workspace_id == WORKSPACE
    assert parsed.kind == "project"
    assert parsed.document_id == DOCUMENT


def test_filename_slugifies_a_colon_and_offset_bearing_stable_key() -> None:
    """A daily digest's stable_key is a capture-window-end ISO timestamp
    (docs/DESIGN.md 6.3), which contains ':' and '+' - unsafe on at least one
    deployment target (docs/DESIGN.md 13's Windows/WSL2 development target)."""
    filename = khoj_filename(
        workspace_id=WORKSPACE,
        kind="daily_digest",
        stable_key="2026-08-30T20:00:00+07:00",
        document_id=DOCUMENT,
    )

    assert ":" not in filename.rsplit("/", maxsplit=1)[-1].rsplit("--", maxsplit=1)[0]
    parsed = parse_khoj_filename(filename)
    assert parsed is not None
    assert parsed.document_id == DOCUMENT


def test_parse_rejects_a_filename_this_project_never_produced() -> None:
    assert parse_khoj_filename("not/a/real-file.md") is None
    assert parse_khoj_filename("only-one-part.md") is None
    assert parse_khoj_filename(f"{WORKSPACE}/project/slug-not-a-uuid.md") is None
    assert parse_khoj_filename(f"not-a-uuid/project/slug--{DOCUMENT}.md") is None


def test_markdown_front_matter_is_valid_yaml_with_the_documented_fields() -> None:
    revision_id = uuid.uuid4()
    window_start = dt.datetime(2026, 8, 29, 20, 0, 0, tzinfo=dt.UTC)
    window_end = dt.datetime(2026, 8, 30, 20, 0, 0, tzinfo=dt.UTC)

    content = khoj_markdown(
        document_id=DOCUMENT,
        revision_id=revision_id,
        workspace_id=WORKSPACE,
        kind="project",
        stable_key="project:thought-capture-ai",
        title='A "quoted" title',
        body_markdown="## Summary\nsome body",
        window_start=window_start,
        window_end=window_end,
        entities=("Thought Capture AI", "Khoj"),
        source_thought_ids=(101, 105),
    )

    text = content.decode("utf-8")
    assert text.startswith("---\n")
    front_matter_text, _, body = text[4:].partition("\n---\n")
    front_matter = yaml.safe_load(front_matter_text)

    assert front_matter["document_id"] == str(DOCUMENT)
    assert front_matter["revision_id"] == str(revision_id)
    assert front_matter["workspace_id"] == str(WORKSPACE)
    assert front_matter["kind"] == "project"
    assert front_matter["stable_key"] == "project:thought-capture-ai"
    assert front_matter["window_start"] == window_start.isoformat()
    assert front_matter["window_end"] == window_end.isoformat()
    assert front_matter["entities"] == ["Thought Capture AI", "Khoj"]
    assert front_matter["source_thought_ids"] == [101, 105]
    assert '# A "quoted" title' in body
    assert "some body" in body


def test_markdown_handles_missing_window_bounds() -> None:
    content = khoj_markdown(
        document_id=DOCUMENT,
        revision_id=uuid.uuid4(),
        workspace_id=WORKSPACE,
        kind="topic",
        stable_key="topic:x",
        title="X",
        body_markdown="body",
        window_start=None,
        window_end=None,
        entities=(),
        source_thought_ids=(),
    )
    text = content.decode("utf-8")
    front_matter_text, _, _ = text[4:].partition("\n---\n")
    front_matter = yaml.safe_load(front_matter_text)
    assert front_matter["window_start"] is None
    assert front_matter["window_end"] is None
    assert front_matter["entities"] == []
    assert front_matter["source_thought_ids"] == []
