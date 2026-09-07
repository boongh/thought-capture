"""``POST /v1/ask`` (docs/DESIGN.md 7.6, docs/adr/0010)."""

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
    """TC_ASK_ENABLED defaults to false (docs/adr/0010) - this must be
    explicit, never a fabricated or empty-looking answer."""
    response = await api_fresh.post("/v1/ask", headers=AUTH, json={"q": "anything"})

    assert response.status_code == 200
    body = response.json()
    assert body == {"enabled": False, "degraded": False, "answer": None, "references": []}


async def test_ask_degrades_when_enabled_but_khoj_is_not_running(
    api_fresh_ask_enabled: httpx.AsyncClient,
) -> None:
    response = await api_fresh_ask_enabled.post("/v1/ask", headers=AUTH, json={"q": "anything"})

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["degraded"] is True
    assert body["answer"] is None
