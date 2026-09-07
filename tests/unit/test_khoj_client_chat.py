"""``HttpKhojClient.chat`` (docs/DESIGN.md 7.6, docs/adr/0010).

The request/response shape asserted here was read directly from
``khoj-ai/khoj``'s source at the pinned tag (``2.0.0-beta.28``) -
``ChatRequestBody`` and the non-streaming branch of ``POST /api/chat`` - not
observed against a live configured instance (docs/adr/0010's "Context"
section explains why that differs from docs/adr/0003's ``/api/search``/
``/api/content`` spike).
"""

from __future__ import annotations

import json

import httpx
import pytest

from tc_domain.khoj_ports import KhojUnavailableError
from tc_infrastructure.khoj.client import HttpKhojClient

UNREACHABLE_URL = "http://127.0.0.1:1"


async def test_chat_raises_khoj_unavailable_when_unreachable() -> None:
    async with httpx.AsyncClient() as http:
        client = HttpKhojClient(http, UNREACHABLE_URL)
        with pytest.raises(KhojUnavailableError):
            await client.chat("anything")


async def test_chat_sends_the_verified_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"response": "hi", "references": {}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpKhojClient(http, "http://khoj.invalid")
        await client.chat("what happened?", limit=3)

    assert captured["url"] == "http://khoj.invalid/api/chat"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["q"] == "/notes what happened?"
    assert body["n"] == 3
    assert body["stream"] is False
    assert body["create_new"] is True


async def test_chat_parses_the_verified_response_shape() -> None:
    payload = {
        "response": "It went well.",
        "references": {
            "context": [
                {"compiled": "…text…", "file": "ws/project/x--doc.md", "heading": "Launch"},
                {"compiled": "…other…", "file": "ws/project/y--doc.md"},  # no "heading"
            ]
        },
        "usage": {"input_tokens": 10},
    }
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpKhojClient(http, "http://khoj.invalid")
        result = await client.chat("q")

    assert result.response == "It went well."
    assert len(result.references) == 2
    assert result.references[0].compiled == "…text…"
    assert result.references[0].filename == "ws/project/x--doc.md"
    assert result.references[0].heading == "Launch"
    assert result.references[1].heading == ""


async def test_chat_treats_missing_references_as_empty() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"response": "ok"}))
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpKhojClient(http, "http://khoj.invalid")
        result = await client.chat("q")

    assert result.response == "ok"
    assert result.references == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"references": {}},  # missing "response" entirely
        {"response": {"image": "…"}},  # a dict-shaped response, not text
        {
            "response": "ok",
            "references": {"context": [{"file": "x"}]},
        },  # reference missing "compiled"
    ],
    ids=["missing_response", "non_text_response", "reference_missing_compiled"],
)
async def test_chat_raises_khoj_unavailable_on_a_malformed_response(payload: object) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpKhojClient(http, "http://khoj.invalid")
        with pytest.raises(KhojUnavailableError):
            await client.chat("q")
