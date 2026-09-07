"""``POST /v1/ask`` (docs/DESIGN.md 7.6, docs/adr/0003's "Ask proxy" amendment)."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.integration.conftest import API_TOKEN

pytestmark = pytest.mark.integration

AUTH = {"Authorization": f"Bearer {API_TOKEN}"}


def _events(response: httpx.Response) -> list[dict[str, object]]:
    """One dict per newline-delimited ``AskEvent`` line (docs/DESIGN.md 7.6:
    "the gateway streams the answer")."""
    return [json.loads(line) for line in response.text.strip().splitlines() if line]


async def test_ask_requires_auth(api_fresh: httpx.AsyncClient) -> None:
    response = await api_fresh.post("/v1/ask", json={"q": "anything"})
    assert response.status_code == 401


async def test_ask_reports_not_enabled_by_default(api_fresh: httpx.AsyncClient) -> None:
    """TC_ASK_ENABLED defaults to false - this must be explicit, never a
    fabricated or empty-looking answer. A single terminal event, since no
    LLM call is ever attempted."""
    response = await api_fresh.post("/v1/ask", headers=AUTH, json={"q": "anything"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = _events(response)
    assert events == [
        {
            "enabled": False,
            "degraded": False,
            "strict_unsupported": False,
            "delta": "",
            "references": None,
            "fallback": None,
            "done": True,
        }
    ]


async def test_ask_degrades_when_enabled_but_khoj_is_unreachable(
    api_fresh_ask_enabled: httpx.AsyncClient,
) -> None:
    response = await api_fresh_ask_enabled.post("/v1/ask", headers=AUTH, json={"q": "anything"})

    assert response.status_code == 200
    events = _events(response)
    assert len(events) == 1
    assert events[0]["enabled"] is True
    assert events[0]["degraded"] is True
    assert events[0]["done"] is True


async def test_ask_falls_back_to_exact_search_when_a_filter_is_requested(
    api_fresh_ask_enabled: httpx.AsyncClient,
) -> None:
    """docs/DESIGN.md 7.6: strict filters return a clear capability signal
    and exact-search results instead of silently querying Khoj's whole
    corpus - no Khoj call happens at all (unlike the plain-question case
    above, this must not degrade even though Khoj is unreachable, since Khoj
    is never contacted)."""
    response = await api_fresh_ask_enabled.post(
        "/v1/ask", headers=AUTH, json={"q": "anything", "kind": "project"}
    )

    assert response.status_code == 200
    events = _events(response)
    assert len(events) == 1
    assert events[0]["enabled"] is True
    assert events[0]["degraded"] is False
    assert events[0]["strict_unsupported"] is True
    assert events[0]["fallback"] is not None
    assert events[0]["fallback"]["items"] == []


async def test_ask_rejects_an_empty_question(api_fresh: httpx.AsyncClient) -> None:
    response = await api_fresh.post("/v1/ask", headers=AUTH, json={"q": ""})
    assert response.status_code == 422


async def test_ask_rejects_a_question_over_the_length_limit(api_fresh: httpx.AsyncClient) -> None:
    response = await api_fresh.post("/v1/ask", headers=AUTH, json={"q": "x" * 2001})
    assert response.status_code == 422
