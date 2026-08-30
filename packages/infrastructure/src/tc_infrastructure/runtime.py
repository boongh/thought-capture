"""Event loop selection.

psycopg's async mode refuses to run on Windows' default ``ProactorEventLoop``
and requires a selector-based loop. The services run in Linux containers in
anything resembling production (docs/DESIGN.md 13), where this is a no-op - but
the development target is Windows, and a developer running the bot or worker
locally would otherwise get an ``InterfaceError`` on the first query.

Every asynchronous entrypoint starts through ``run``.
"""

from __future__ import annotations

import asyncio
import os
import selectors
from collections.abc import Callable, Coroutine
from typing import Any


def event_loop_factory() -> Callable[[], asyncio.AbstractEventLoop]:
    """Return a loop factory that psycopg can use on this platform.

    The check reads ``os.name`` rather than ``sys.platform`` deliberately. mypy
    treats ``sys.platform`` comparisons as platform narrowing and marks the
    other branch unreachable, so the file could only satisfy strict mode on one
    platform at a time: the ignore that Windows needs is an unused-ignore error
    on Linux, and vice versa. ``os.name`` is not narrowed, so both branches stay
    live for the type checker while behaving identically at runtime.
    """
    if os.name == "nt":
        # psycopg's async mode raises InterfaceError on a ProactorEventLoop.
        return lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
    # On Linux, where the services actually run, the default loop is already
    # selector-based and this is a no-op.
    return asyncio.new_event_loop


def run[T](main: Coroutine[Any, Any, T]) -> T:
    """``asyncio.run`` with a loop the database driver can actually use."""
    return asyncio.run(main, loop_factory=event_loop_factory())
