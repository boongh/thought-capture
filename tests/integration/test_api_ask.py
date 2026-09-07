"""``POST /v1/ask`` (docs/DESIGN.md 7.6, docs/adr/0003's "Ask proxy" amendment)."""

from __future__ import annotations

import httpx
import pytest

from tests.integration.conftest import API_TOKEN

pytestmark = pytest.mark.integration

AUTH = {"Authorization": f"Bearer {API_TOKEN}"}


async def test_ask_requires_auth(api_fresh: httpx.AsyncClient) -> None:
    response = await api_fresh.post("/v1/ask", json={"q": "anything"})
    assert response.status_code == 401


async def test_ask_reports_not_enabled_by_default(api_fresh: httpx.AsyncClient) -> None:
    """TC_ASK_ENABLED defaults to false - this must be explicit, never a
    fabricated or empty-looking answer."""
    response = await api_fresh.post("/v1/ask", headers=AUTH, json={"q": "anything"})

    assert response.status_code == 200
    body = response.json()
    assert body == {"enabled": False, "degraded": False, "answer": None, "references": []}


async def test_ask_degrades_when_enabled_but_khoj_is_unreachable(
    api_fresh_ask_enabled: httpx.AsyncClient,
) -> None:
    response = await api_fresh_ask_enabled.post("/v1/ask", headers=AUTH, json={"q": "anything"})

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["degraded"] is True
    assert body["answer"] is None


async def test_ask_rejects_an_empty_question(api_fresh: httpx.AsyncClient) -> None:
    response = await api_fresh.post("/v1/ask", headers=AUTH, json={"q": ""})
    assert response.status_code == 422


async def test_ask_rejects_a_question_over_the_length_limit(api_fresh: httpx.AsyncClient) -> None:
    response = await api_fresh.post("/v1/ask", headers=AUTH, json={"q": "x" * 2001})
    assert response.status_code == 422
