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
import selectors
import sys
from collections.abc import Callable, Coroutine
from typing import Any


def event_loop_factory() -> Callable[[], asyncio.AbstractEventLoop]:
    """Return a loop factory that psycopg can use on this platform."""
    if sys.platform == "win32":
        return lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
    # mypy narrows sys.platform to the platform it is running on, so on Windows
    # it considers this branch dead. It is the branch that runs in the Linux
    # containers, where the default loop is already selector-based.
    return asyncio.new_event_loop  # type: ignore[unreachable]


def run[T](main: Coroutine[Any, Any, T]) -> T:
    """``asyncio.run`` with a loop the database driver can actually use."""
    return asyncio.run(main, loop_factory=event_loop_factory())
