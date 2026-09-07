"""Window resolution and formatting for the ``/organize``/``/status``/
``/search``/``/ask`` slash commands (tc_discord_bot.commands).

These are pure functions with no Discord Gateway dependency, so they are
tested directly rather than through a live interaction, the same way
``tc_discord_bot.adapter`` is tested with lightweight stand-ins instead of
real ``discord`` objects.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from tc_discord_bot.commands import (
    _format_ask_answer,
    _format_run,
    _format_search_page,
    _resolve_window,
    _WindowInputError,
)
from tc_domain.ask import AskAnswer, AskReference
from tc_domain.capture import WorkspaceId
from tc_domain.search import SearchPage, SearchResult
from tc_domain.windows import CaptureWindow
from tc_infrastructure.db.run_reader import RunRecord

WORKSPACE_ID = WorkspaceId(uuid.UUID("11111111-1111-4111-8111-111111111111"))
TIMEZONE = "Asia/Bangkok"
DIGEST_TIME = dt.time(20, 0)


async def _no_captures(_: WorkspaceId) -> dt.datetime | None:
    return None


def _captured_at(instant: dt.datetime):
    async def lookup(_: WorkspaceId) -> dt.datetime | None:
        return instant

    return lookup


async def test_explicit_from_and_to_build_the_requested_window() -> None:
    window = await _resolve_window(
        "2026-08-30T20:00:00+07:00",
        "2026-08-31T20:00:00+07:00",
        first_capture_at=_no_captures,
        workspace_id=WORKSPACE_ID,
        digest_local_time=DIGEST_TIME,
        timezone=TIMEZONE,
        clock=lambda: dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
    )
    assert window == CaptureWindow(
        start=dt.datetime(2026, 8, 30, 20, 0, tzinfo=dt.timezone(dt.timedelta(hours=7))),
        end=dt.datetime(2026, 8, 31, 20, 0, tzinfo=dt.timezone(dt.timedelta(hours=7))),
    )


async def test_only_one_of_from_or_to_is_rejected() -> None:
    with pytest.raises(_WindowInputError, match="both"):
        await _resolve_window(
            "2026-08-30T20:00:00+07:00",
            None,
            first_capture_at=_no_captures,
            workspace_id=WORKSPACE_ID,
            digest_local_time=DIGEST_TIME,
            timezone=TIMEZONE,
            clock=lambda: dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        )


async def test_a_naive_from_to_is_rejected_for_missing_an_offset() -> None:
    with pytest.raises(_WindowInputError, match="offset"):
        await _resolve_window(
            "2026-08-30T20:00:00",
            "2026-08-31T20:00:00",
            first_capture_at=_no_captures,
            workspace_id=WORKSPACE_ID,
            digest_local_time=DIGEST_TIME,
            timezone=TIMEZONE,
            clock=lambda: dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        )


async def test_an_unparsable_timestamp_is_rejected() -> None:
    with pytest.raises(_WindowInputError, match="ISO 8601"):
        await _resolve_window(
            "not-a-timestamp",
            "2026-08-31T20:00:00+07:00",
            first_capture_at=_no_captures,
            workspace_id=WORKSPACE_ID,
            digest_local_time=DIGEST_TIME,
            timezone=TIMEZONE,
            clock=lambda: dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        )


async def test_an_end_before_start_is_rejected() -> None:
    with pytest.raises(_WindowInputError):
        await _resolve_window(
            "2026-08-31T20:00:00+07:00",
            "2026-08-30T20:00:00+07:00",
            first_capture_at=_no_captures,
            workspace_id=WORKSPACE_ID,
            digest_local_time=DIGEST_TIME,
            timezone=TIMEZONE,
            clock=lambda: dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        )


async def test_no_captures_yet_means_no_window() -> None:
    window = await _resolve_window(
        None,
        None,
        first_capture_at=_no_captures,
        workspace_id=WORKSPACE_ID,
        digest_local_time=DIGEST_TIME,
        timezone=TIMEZONE,
        clock=lambda: dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
    )
    assert window is None


async def test_omitting_both_defaults_to_the_current_open_window() -> None:
    # Bangkok is UTC+7; 2026-08-31T14:00Z is 21:00 local, past the 20:00
    # cutoff, so the open window started at today's 20:00 local cutoff.
    now = dt.datetime(2026, 8, 31, 14, 0, tzinfo=dt.UTC)
    window = await _resolve_window(
        None,
        None,
        first_capture_at=_captured_at(dt.datetime(2026, 8, 1, tzinfo=dt.UTC)),
        workspace_id=WORKSPACE_ID,
        digest_local_time=DIGEST_TIME,
        timezone=TIMEZONE,
        clock=lambda: now,
    )
    assert window is not None
    assert window.end == now
    assert window.start == dt.datetime(2026, 8, 31, 13, 0, tzinfo=dt.UTC)  # 20:00 +07 == 13:00Z


async def test_omitting_both_immediately_after_the_first_capture_is_a_short_window() -> None:
    """The open window cannot start before the workspace's own first capture,
    even though the naive "most recent cutoff" math would put it a full day
    earlier - this is the one case ``organized_frontier`` normally handles,
    approximated here without a database round trip."""
    first_capture = dt.datetime(2026, 8, 31, 13, 30, tzinfo=dt.UTC)  # just after the 13:00Z cutoff
    now = first_capture + dt.timedelta(minutes=5)
    window = await _resolve_window(
        None,
        None,
        first_capture_at=_captured_at(first_capture),
        workspace_id=WORKSPACE_ID,
        digest_local_time=DIGEST_TIME,
        timezone=TIMEZONE,
        clock=lambda: now,
    )
    assert window is not None
    assert window.start == dt.datetime(2026, 8, 31, 13, 0, tzinfo=dt.UTC)
    assert window.end == now


def _run_record(**overrides: object) -> RunRecord:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "kind": "organize",
        "status": "succeeded",
        "window_start": dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC),
        "window_end": dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC),
        "prompt_version": "organize-v1",
        "model_provider": "offline",
        "model_id": "offline-model",
        "input_tokens": 10,
        "output_tokens": 5,
        "estimated_cost_usd": None,
        "context_recall": None,
        "context_degraded": False,
        "started_at": dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC),
        "finished_at": dt.datetime(2026, 8, 31, 13, 1, tzinfo=dt.UTC),
        "error_code": None,
        "error_detail": None,
        "created_at": dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC),
    }
    defaults.update(overrides)
    return RunRecord(**defaults)  # type: ignore[arg-type]


def test_format_run_surfaces_the_error_for_a_failed_run() -> None:
    record = _run_record(
        status="failed", error_code="LLMError", error_detail="provider call failed"
    )
    text = _format_run(record)
    assert "failed" in text
    assert "LLMError" in text
    assert "provider call failed" in text


def test_format_run_flags_a_degraded_context() -> None:
    record = _run_record(context_degraded=True)
    assert "degraded" in _format_run(record)


def test_format_run_omits_model_line_for_an_empty_window_run() -> None:
    """OrganizeWindow's empty-window fast path never calls a model."""
    record = _run_record(model_id=None, model_provider=None, input_tokens=0, output_tokens=0)
    text = _format_run(record)
    assert "Model:" not in text


def _search_result(**overrides: object) -> SearchResult:
    defaults: dict[str, object] = {
        "result_id": "r1",
        "document_id": uuid.uuid4(),
        "revision_id": uuid.uuid4(),
        "thought_ids": (),
        "kind": "project",
        "title": "A project",
        "snippet": "a snippet",
        "updated_at": dt.datetime(2026, 8, 31, tzinfo=dt.UTC),
        "entities": (),
        "channels": ("exact",),
        "rank": 1.0,
    }
    defaults.update(overrides)
    return SearchResult(**defaults)  # type: ignore[arg-type]


def test_format_search_page_reports_no_results() -> None:
    page = SearchPage(items=(), next_cursor=None)
    text = _format_search_page(page, "exact")
    assert "No results" in text


def test_format_search_page_lists_title_channels_and_snippet() -> None:
    result = _search_result(title="My Project", channels=("exact", "semantic"), snippet="hello")
    page = SearchPage(items=(result,), next_cursor=None)
    text = _format_search_page(page, "hybrid")
    assert "My Project" in text
    assert "exact+semantic" in text
    assert "hello" in text
    assert str(result.document_id) in text


def test_format_search_page_flags_degraded() -> None:
    page = SearchPage(items=(), next_cursor=None, degraded=True)
    text = _format_search_page(page, "hybrid")
    assert "degraded" in text.lower()


def test_format_ask_answer_when_not_enabled() -> None:
    answer = AskAnswer(enabled=False, degraded=False, answer=None, references=())
    text = _format_ask_answer(answer)
    assert "not enabled" in text.lower()
    assert "/search" in text


def test_format_ask_answer_when_degraded() -> None:
    answer = AskAnswer(enabled=True, degraded=True, answer=None, references=())
    text = _format_ask_answer(answer)
    assert "could not answer" in text.lower()


def test_format_ask_answer_with_references() -> None:
    reference = AskReference(document_id=uuid.uuid4(), title="A Doc", snippet="…")
    answer = AskAnswer(
        enabled=True, degraded=False, answer="The launch went well.", references=(reference,)
    )
    text = _format_ask_answer(answer)
    assert "The launch went well." in text
    assert "A Doc" in text
    assert str(reference.document_id) in text


def test_format_ask_answer_truncates_to_the_discord_message_limit() -> None:
    answer = AskAnswer(enabled=True, degraded=False, answer="x" * 3000, references=())
    text = _format_ask_answer(answer)
    assert len(text) <= 2000
    assert text.endswith("(truncated)")
