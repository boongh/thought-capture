"""``HttpKhojClient`` error handling - no live Khoj needed (docs/adr/0003)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from tc_domain.khoj_ports import KhojIndexFile, KhojUnavailableError
from tc_infrastructure.khoj.client import HttpKhojClient

# Port 1 is a privileged, essentially-never-listening port: connecting to it
# reliably fails fast without depending on a specific unreachable-host DNS/
# firewall behavior.
UNREACHABLE_URL = "http://127.0.0.1:1"


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def client(http: httpx.AsyncClient) -> HttpKhojClient:
    return HttpKhojClient(http, UNREACHABLE_URL)


async def test_index_raises_khoj_unavailable_when_unreachable(client: HttpKhojClient) -> None:
    with pytest.raises(KhojUnavailableError):
        await client.index((KhojIndexFile(filename="a.md", content=b"x"),))


async def test_delete_raises_khoj_unavailable_when_unreachable(client: HttpKhojClient) -> None:
    with pytest.raises(KhojUnavailableError):
        await client.delete(("a.md",))


async def test_search_raises_khoj_unavailable_when_unreachable(client: HttpKhojClient) -> None:
    with pytest.raises(KhojUnavailableError):
        await client.search("anything")


async def test_index_is_a_no_op_for_an_empty_tuple(client: HttpKhojClient) -> None:
    """Never even attempts the unreachable connection - proves this is a
    genuine no-op, not a raised-and-ignored error."""
    await client.index(())


async def test_delete_is_a_no_op_for_an_empty_tuple(client: HttpKhojClient) -> None:
    await client.delete(())


@pytest.mark.parametrize(
    "payload",
    [
        [{"score": 0.5}],  # missing "entry"
        [{"entry": "text", "score": "not-a-number"}],  # score is not numeric
        {"entry": "text", "score": 0.5},  # a dict, not a list of results
    ],
    ids=["missing_entry", "non_numeric_score", "non_list_payload"],
)
async def test_search_raises_khoj_unavailable_on_a_malformed_response(payload: object) -> None:
    """A shape Khoj's own documented API would never return is exactly as
    unusable to a caller as an unreachable server (docs/DESIGN.md 7.5) - it
    must not surface as a bare KeyError/TypeError a caller's
    ``except KhojUnavailableError`` would miss."""
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as http:
        malformed_client = HttpKhojClient(http, "http://khoj.invalid")
        with pytest.raises(KhojUnavailableError):
            await malformed_client.search("anything")
