"""Test-wide configuration.

psycopg's async mode refuses to run on Windows' default ``ProactorEventLoop``,
so pytest-asyncio is pointed at the same loop factory the real entrypoints use
(``tc_infrastructure.runtime``). Without this, every asynchronous database test
fails on Windows with ``InterfaceError``.

This uses pytest-asyncio's ``pytest_asyncio_loop_factories`` hook rather than
the ``event_loop_policy`` fixture, because ``asyncio`` event loop policies are
deprecated in Python 3.14 and scheduled for removal in 3.16.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from tc_infrastructure.runtime import event_loop_factory


def pytest_asyncio_loop_factories(
    config: object, item: object
) -> Mapping[str, Callable[[], object]]:
    """Supply the platform-appropriate event loop to every async test."""
    return {"tc": event_loop_factory()}
