"""``HttpKhojClient.chat`` (docs/DESIGN.md 7.6, docs/adr/0003's "Ask proxy"
amendment).

The streaming wire format asserted here was read directly from
``khoj-ai/khoj``'s source at the pinned tag (``2.0.0-beta.28``) -
``ChatEvent`` (``src/khoj/processor/conversation/utils.py``, delimiter
``END_EVENT``) and ``send_event``'s serialization
(``src/khoj/routers/api_chat.py``) - not observed against a live configured
instance (see that ADR amendment's own "Context" for why that differs from
the rest of this adapter's evidence standard, and names the follow-up
contract test that closes the gap). The unconfigured-instance failure path
*was* live-verified separately (a plain HTTP 500) and is exercised by the
"unreachable"/"http error" tests below via the same mechanism the old
buffered implementation used.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tc_domain.khoj_ports import KhojUnavailableError
from tc_infrastructure.khoj.client import HttpKhojClient

UNREACHABLE_URL = "http://127.0.0.1:1"
_END_EVENT = "␃\U0001f51a␗"


def _event(event_type: str, data: object) -> str:
    return json.dumps({"type": event_type, "data": data}, ensure_ascii=False) + _END_EVENT


def _message(text: str) -> str:
    """A MESSAGE event's payload is raw, unwrapped text (khoj's own
    `send_event`), not JSON."""
    return text + _END_EVENT


async def _drain(client: HttpKhojClient, question: str = "q"):  # type: ignore[no-untyped-def]
    return [chunk async for chunk in client.chat(question)]


async def test_chat_raises_khoj_unavailable_when_unreachable() -> None:
    async with httpx.AsyncClient() as http:
        client = HttpKhojClient(http, UNREACHABLE_URL)
        with pytest.raises(KhojUnavailableError):
            await _drain(client)


async def test_chat_raises_khoj_unavailable_on_an_http_error_status() -> None:
    """Matches the live-verified unconfigured-instance behavior: a plain
    500 with no JSON body, raised before any chunk is ever yielded."""
    transport = httpx.MockTransport(lambda _: httpx.Response(500))
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpKhojClient(http, "http://khoj.invalid")
        with pytest.raises(KhojUnavailableError):
            await _drain(client)


async def test_chat_sends_the_verified_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, text=_event("end_response", ""))
        return httpx.Response(200)  # the DELETE cleanup call, if any

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpKhojClient(http, "http://khoj.invalid")
        await _drain(client, "what happened?")

    assert captured["url"] == "http://khoj.invalid/api/chat"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["q"] == "/notes what happened?"
    assert body["n"] == 5
    assert body["stream"] is True
    assert body["create_new"] is True


async def test_chat_streams_text_deltas_in_order_and_resolves_references() -> None:
    stream_body = "".join(
        [
            _event("metadata", {"conversationId": "conv-1", "turnId": "turn-1"}),
            _event(
                "references",
                {
                    "context": [
                        {"compiled": "…text…", "file": "ws/project/x--doc.md", "heading": "Launch"},
                        {"compiled": "…other…", "file": "ws/project/y--doc.md"},  # no "heading"
                    ]
                },
            ),
            _message("It went "),
            _message("well."),
            _event("end_response", ""),
        ]
    )
    delete_calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            delete_calls.append(request.url.params.get("conversation_id"))
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, text=stream_body)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpKhojClient(http, "http://khoj.invalid")
        chunks = await _drain(client)

    text_deltas = [c.text_delta for c in chunks if c.text_delta]
    assert text_deltas == ["It went ", "well."]  # streamed in order, not pre-joined

    reference_chunks = [c for c in chunks if c.references is not None]
    assert len(reference_chunks) == 1
    references = reference_chunks[0].references
    assert references is not None
    assert len(references) == 2
    assert references[0].compiled == "…text…"
    assert references[0].filename == "ws/project/x--doc.md"
    assert references[0].heading == "Launch"
    assert references[1].heading == ""

    assert chunks[-1].done is True

    # Conversation retention (docs/adr/0003's "Ask proxy" amendment):
    # deleted immediately after use, using the id from the metadata event.
    assert delete_calls == ["conv-1"]


async def test_chat_ignores_event_types_it_does_not_need() -> None:
    stream_body = "".join(
        [
            _event("status", "thinking"),
            _event("start_llm_response", ""),
            _message("answer"),
            _event("end_llm_response", ""),
            _event("usage", {"input_tokens": 10}),
            _event("end_response", ""),
        ]
    )
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=stream_body))
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpKhojClient(http, "http://khoj.invalid")
        chunks = await _drain(client)

    assert [c.text_delta for c in chunks if c.text_delta] == ["answer"]
    assert all(c.references is None for c in chunks)


async def test_chat_does_not_drop_a_raw_answer_chunk_that_collides_with_a_known_event_type() -> (
    None
):
    """The exact P1 independent review caught: a raw MESSAGE chunk streamed
    *during* the answer (i.e. after `start_llm_response`, before
    `end_llm_response`) whose text itself happens to be valid JSON with a
    "type" key that collides with a real, otherwise-ignored khoj event name
    (e.g. the model answers with literal JSON like
    '{"type": "status", "data": "42"}') must still be surfaced as answer
    text - the live-verified protocol (docs/adr/0003) never emits `status`
    inside that window, so this cannot be a genuine event there, unlike the
    same shape arriving before `start_llm_response`."""
    stream_body = "".join(
        [
            _event("start_llm_response", ""),
            _message('{"type": "status", "data": "42"}'),
            _event("end_llm_response", ""),
            _event("end_response", ""),
        ]
    )
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=stream_body))
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpKhojClient(http, "http://khoj.invalid")
        chunks = await _drain(client)

    assert [c.text_delta for c in chunks if c.text_delta] == ['{"type": "status", "data": "42"}']


async def test_chat_still_ignores_a_genuine_status_event_before_the_answer_starts() -> None:
    """The companion case: the same `status` shape arriving *before*
    `start_llm_response` (where khoj actually emits it, per the live
    contract capture) is a genuine control event and must still be
    silently ignored, not surfaced as answer text."""
    stream_body = "".join(
        [
            _event("status", "**Searching Documents for:** q"),
            _event("start_llm_response", ""),
            _message("answer"),
            _event("end_llm_response", ""),
            _event("end_response", ""),
        ]
    )
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=stream_body))
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpKhojClient(http, "http://khoj.invalid")
        chunks = await _drain(client)

    assert [c.text_delta for c in chunks if c.text_delta] == ["answer"]


async def test_chat_treats_a_json_shaped_message_event_as_text() -> None:
    stream_body = _event("message", {"details": "something"}) + _event("end_response", "")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=stream_body))
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpKhojClient(http, "http://khoj.invalid")
        chunks = await _drain(client)

    deltas = [c.text_delta for c in chunks if c.text_delta]
    assert deltas == [json.dumps({"details": "something"})]


async def test_chat_treats_a_json_shaped_raw_answer_as_text_not_a_dropped_event() -> None:
    """The exact bug independent review caught: a raw (untyped) MESSAGE
    chunk whose answer text itself happens to be a JSON object - e.g. the
    model literally answers '{"answer": "..."}' - has no "type" key, so it
    must never be mistaken for a typed event and silently dropped."""
    stream_body = _message('{"answer": "42"}') + _event("end_response", "")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=stream_body))
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpKhojClient(http, "http://khoj.invalid")
        chunks = await _drain(client)

    assert [c.text_delta for c in chunks if c.text_delta] == ['{"answer": "42"}']


async def test_chat_treats_a_json_shaped_raw_answer_with_a_bogus_type_key_as_text() -> None:
    """The same bug, one layer deeper: an untyped answer chunk that
    coincidentally has a "type" key khoj never actually emits (e.g. the
    model answers with literal JSON like '{"type": "answer", "data": "42"}')
    must not be mistaken for a genuine-but-unhandled khoj event and dropped
    - only a *recognized* event type may be silently ignored."""
    stream_body = _message('{"type": "answer", "data": "42"}') + _event("end_response", "")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=stream_body))
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpKhojClient(http, "http://khoj.invalid")
        chunks = await _drain(client)

    assert [c.text_delta for c in chunks if c.text_delta] == ['{"type": "answer", "data": "42"}']


async def test_chat_treats_a_json_shaped_raw_answer_with_a_non_string_type_as_text() -> None:
    """A deeper variant of the same bug: a raw answer chunk whose JSON shape
    has a "type" key, but one whose *value* is not even a string (e.g. a
    list or dict) - a real khoj event's "type" is always a string, so this
    cannot be a genuine event either. Must degrade to text like every other
    unrecognized shape, not raise `TypeError: unhashable type` from testing
    an unhashable value for frozenset membership."""
    stream_body = _message('{"type": ["string", "null"], "data": "x"}') + _event("end_response", "")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=stream_body))
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpKhojClient(http, "http://khoj.invalid")
        chunks = await _drain(client)  # must not raise TypeError

    assert [c.text_delta for c in chunks if c.text_delta] == [
        '{"type": ["string", "null"], "data": "x"}'
    ]


async def test_chat_cleanup_failure_does_not_mask_a_successful_answer() -> None:
    stream_body = "".join(
        [
            _event("metadata", {"conversationId": "conv-1", "turnId": "t"}),
            _message("answer"),
            _event("end_response", ""),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(500)
        return httpx.Response(200, text=stream_body)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpKhojClient(http, "http://khoj.invalid")
        chunks = await _drain(client)  # must not raise

    assert [c.text_delta for c in chunks if c.text_delta] == ["answer"]


@pytest.mark.parametrize(
    "references_data",
    [
        ["not", "a", "dict"],  # the exact shape independent review caught
        {"context": "not-a-list"},
        {"context": [{"file": "x"}]},  # missing "compiled"
        {"context": ["not-a-dict"]},
    ],
    ids=[
        "references_data_is_a_list",
        "context_not_a_list",
        "item_missing_compiled",
        "item_not_a_dict",
    ],
)
async def test_chat_raises_khoj_unavailable_on_a_malformed_references_event(
    references_data: object,
) -> None:
    """Independent review: a malformed `references` shape must degrade like
    every other unusable response, never surface as a bare, uncaught
    `AttributeError`."""
    stream_body = _event("references", references_data) + _event("end_response", "")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=stream_body))
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpKhojClient(http, "http://khoj.invalid")
        with pytest.raises(KhojUnavailableError):
            await _drain(client)
