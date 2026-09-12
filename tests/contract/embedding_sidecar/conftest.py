"""Fixtures for the embedding sidecar's contract tests (docs/adr/0010 §5).

Start the server with the contract-test overlay applied - a normal
`--profile core up` no longer publishes this service to any host port at all
(deploy/compose/docker-compose.yml's own header comment on `embedding-sidecar`
has the full reasoning), so the base file alone leaves nothing on
TC_EMBEDDING_SIDECAR_HOST_BASE_URL's default for these tests to reach.

This is deliberately a DIFFERENT variable from TC_EMBEDDING_SIDECAR_BASE_URL
(env.example, forwarded into the `worker` service by
deploy/compose/docker-compose.yml) - that one is the URL a *container* uses
to reach the sidecar over the `embedding` Compose network, and defaults to
the in-container service name. This one is the URL the *host* uses once the
contract-test overlay below publishes the sidecar's port to loopback.
Collapsing them into one name previously meant the loopback default silently
broke the worker container, since nothing inside a container listens on its
own 127.0.0.1 (review finding F12):

    docker compose --env-file .env -f deploy/compose/docker-compose.yml \
      -f deploy/compose/embedding-sidecar.contract-test.docker-compose.yml \
      --profile core up -d --build embedding-sidecar

Unlike `tests/contract/khoj/`, there is no first-party adapter class to test
here yet - `EmbeddingPort`/`HttpEmbeddingClient` are a later slice
(docs/adr/0010's migration step 2). This slice proves the sidecar's raw HTTP
contract in isolation, so these tests speak plain `httpx` against the running
container directly.

A connection failure is deliberately allowed to propagate as an error, the
same choice `tests/integration/conftest.py` and `tests/contract/khoj/conftest.py`
make: a skip would let an unreachable sidecar produce a green run.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest

EMBEDDING_SIDECAR_BASE_URL = os.environ.get(
    "TC_EMBEDDING_SIDECAR_HOST_BASE_URL", "http://127.0.0.1:8081"
)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Refuse to report success when required contract tests were skipped.

    Mirrors `tests/contract/khoj/conftest.py`'s equivalent hook for
    TC_REQUIRE_CONTRACT.
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
async def sidecar() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=EMBEDDING_SIDECAR_BASE_URL, timeout=30.0) as client:
        yield client
