"""Debug page HTML: escaping and structure, without a database or app (docs/adr/0009)."""

from __future__ import annotations

import datetime as dt
import uuid

from tc_api import debug_templates
from tc_domain.capture import ThoughtId
from tc_infrastructure.db.document_reader import DocumentSummary
from tc_infrastructure.db.entity_reader import EntityRecord
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
    html = debug_templates.thoughts_page([record], next_cursor=None, token=None)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_thoughts_page_includes_a_next_link_only_when_there_is_a_cursor() -> None:
    with_cursor = debug_templates.thoughts_page([], next_cursor="abc.1", token=None)
    without_cursor = debug_templates.thoughts_page([], next_cursor=None, token=None)

    assert "cursor=abc.1" in with_cursor
    assert "Next page" not in without_cursor


def test_the_token_is_carried_into_pagination_and_nav_links() -> None:
    html = debug_templates.thoughts_page([], next_cursor="abc.1", token="secret-token")
    assert "token=secret-token" in html


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
    html = debug_templates.entities_page([record], token=None)

    assert "<img src=x onerror=alert(1)>" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_digests_page_shows_a_placeholder_when_there_are_none() -> None:
    html = debug_templates.digests_page([], {}, token=None)
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
    html = debug_templates.digests_page([summary], bodies, token=None)

    assert "<script>evil()</script>" not in html
    assert "&lt;script&gt;evil()&lt;/script&gt;" in html
    assert "## Summary" in html
