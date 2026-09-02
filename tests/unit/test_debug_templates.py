"""Debug page HTML: escaping and structure, without a database or app (docs/adr/0009)."""

from __future__ import annotations

import datetime as dt
import uuid

from tc_api import debug_templates
from tc_domain.capture import ThoughtId
from tc_infrastructure.db.document_reader import DocumentSummary
from tc_infrastructure.db.entity_reader import EntityRecord
from tc_infrastructure.db.llm_call_reader import LlmCallRecord
from tc_infrastructure.db.thought_reader import ThoughtRecord

CREATED_AT = dt.datetime(2026, 8, 31, 12, tzinfo=dt.UTC)


def a_thought(body: str) -> ThoughtRecord:
    return ThoughtRecord(
        id=ThoughtId(1),
        source="discord",
        source_message_id="msg-1",
        source_channel_id=None,
        body=body,
        client_created_at=CREATED_AT,
        client_timezone="Asia/Bangkok",
        client_local_date=dt.date(2026, 8, 31),
        client_local_time=dt.time(19, 0),
        received_at=CREATED_AT,
        content_language="en",
        correction_of=None,
    )


def test_a_thought_body_containing_html_is_escaped_not_rendered() -> None:
    record = a_thought("<script>alert(1)</script>")
    html = debug_templates.thoughts_page([record], next_cursor=None)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_thoughts_page_includes_a_next_link_only_when_there_is_a_cursor() -> None:
    with_cursor = debug_templates.thoughts_page([], next_cursor="abc.1")
    without_cursor = debug_templates.thoughts_page([], next_cursor=None)

    assert "cursor=abc.1" in with_cursor
    assert "Next page" not in without_cursor


def test_nav_and_pagination_links_never_carry_a_secret() -> None:
    """Basic Auth credentials are cached by the browser per-origin - unlike
    the query-param token this replaced, no page-rendered link ever needs
    to carry the secret, so none of them may.
    """
    html = debug_templates.thoughts_page([], next_cursor="abc.1")
    assert "token=" not in html


def test_an_entity_with_a_malicious_alias_is_escaped() -> None:
    record = EntityRecord(
        id=uuid.uuid4(),
        entity_type="person",
        canonical_name="<b>Name</b>",
        stable_key="person:name",
        aliases=("<img src=x onerror=alert(1)>",),
        mention_count=3,
        last_mentioned_at=CREATED_AT,
        created_at=CREATED_AT,
    )
    html = debug_templates.entities_page([record])

    assert "<img src=x onerror=alert(1)>" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_digests_page_shows_a_placeholder_when_there_are_none() -> None:
    html = debug_templates.digests_page([], {})
    assert "No digests yet." in html


def test_a_digest_body_is_escaped_inside_the_preformatted_block() -> None:
    document_id = uuid.uuid4()
    summary = DocumentSummary(
        id=document_id,
        kind="daily_digest",
        stable_key="daily_digest:2026-08-31",
        title="Daily digest",
        revision_number=1,
        change_summary="created",
        updated_at=CREATED_AT,
    )
    bodies = {document_id: "## Summary\n\n<script>evil()</script>"}
    html = debug_templates.digests_page([summary], bodies)

    assert "<script>evil()</script>" not in html
    assert "&lt;script&gt;evil()&lt;/script&gt;" in html
    assert "## Summary" in html


def a_call(*, reasoning_effort: str | None = None, error_code: str | None = None) -> LlmCallRecord:
    return LlmCallRecord(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        step="organize",
        sequence=1,
        model_requested="vendor/pinned-model",
        model_served="vendor/pinned-model",
        provider="synthetic-provider",
        request_params={"reasoning_effort": reasoning_effort},
        input_tokens=100,
        output_tokens=50,
        estimated_cost_usd=None,
        latency_ms=250,
        error_code=error_code,
        created_at=CREATED_AT,
    )


def test_runs_page_shows_the_reasoning_effort_that_was_sent() -> None:
    html = debug_templates.runs_page([a_call(reasoning_effort="none")], next_cursor=None)
    assert "<td>none</td>" in html


def test_runs_page_shows_a_blank_reasoning_effort_cell_when_unset() -> None:
    html = debug_templates.runs_page([a_call(reasoning_effort=None)], next_cursor=None)
    assert "vendor/pinned-model" in html


def test_runs_page_shows_a_placeholder_when_there_are_none() -> None:
    html = debug_templates.runs_page([], next_cursor=None)
    assert "No LLM calls journaled yet." in html


def test_runs_page_includes_a_next_link_only_when_there_is_a_cursor() -> None:
    with_cursor = debug_templates.runs_page([a_call()], next_cursor="abc.1:2")
    without_cursor = debug_templates.runs_page([a_call()], next_cursor=None)

    assert "cursor=abc.1:2" in with_cursor
    assert "Next page" not in without_cursor


def test_a_run_error_code_is_escaped() -> None:
    html = debug_templates.runs_page(
        [a_call(error_code="<script>evil()</script>")], next_cursor=None
    )
    assert "<script>evil()</script>" not in html
    assert "&lt;script&gt;evil()&lt;/script&gt;" in html
