"""Fixtures for pinned-Khoj contract tests (docs/adr/0003).

Start the server with:

    docker compose --env-file .env -f deploy/compose/docker-compose.yml \
      -f deploy/compose/khoj.docker-compose.yml --profile ai up -d

`tests/contract/khoj/test_khoj_chat.py` additionally needs Khoj configured
with a real (if stubbed) chat model - Khoj only registers one at its own
first `--non-interactive` boot, so a volume that has ever booted without
`TC_KHOJ_OPENAI_BASE_URL`/`TC_KHOJ_OPENAI_API_KEY` set has already decided it
has none. See `tests/contract/khoj/stub_chat_model.py`'s docstring for the
full up-fresh sequence.

A connection failure is deliberately allowed to propagate as an error, the
same choice `tests/integration/conftest.py` makes for PostgreSQL: a skip
would let an unreachable Khoj produce a green run.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from tc_infrastructure.khoj.client import HttpKhojClient

KHOJ_BASE_URL = os.environ.get("TC_KHOJ_BASE_URL", "http://127.0.0.1:42110")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Refuse to report success when required contract tests were skipped.

    Mirrors `tests/integration/conftest.py`'s equivalent hook for
    TC_REQUIRE_INTEGRATION.
    """
    if os.environ.get("TC_REQUIRE_CONTRACT") != "1":
        return

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:  # pragma: no cover - defensive
        return

    skipped = reporter.stats.get("skipped", [])
    passed = reporter.stats.get("passed", [])
    if skipped:
        reporter.write_line(
            f"TC_REQUIRE_CONTRACT=1: {len(skipped)} contract test(s) were skipped; "
            "a skipped contract suite is not verification.",
            red=True,
        )
        session.exitstatus = 1
    elif not passed:
        reporter.write_line("TC_REQUIRE_CONTRACT=1: no contract tests ran.", red=True)
        session.exitstatus = 1


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def khoj_client(http: httpx.AsyncClient) -> HttpKhojClient:
    return HttpKhojClient(http, KHOJ_BASE_URL)


@pytest.fixture
def unique() -> str:
    """A random token for building content/filenames a test can find without
    colliding with another test's or a prior run's leftover data."""
    return uuid.uuid4().hex[:12]
