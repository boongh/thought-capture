"""Sidecar entrypoint.

    python -m tc_embedding_sidecar

Binds to the configured host/port; the compose service publishes no host
port at all (loopback-only pattern, docs/DESIGN.md 12.2) - only other
containers on the internal network can reach it.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from tc_embedding_sidecar.settings import get_settings


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = get_settings()
    uvicorn.run(
        "tc_embedding_sidecar.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level="info",
        # The transport-level concurrency ceiling (settings.py's own
        # docstring has the full reasoning): beyond this many concurrent
        # connections, uvicorn itself answers 503, before this process's
        # own code - including MaxBodySizeMiddleware's body buffering -
        # ever runs for the request past the limit. Confirmed against
        # uvicorn's own source to count a connection from the moment it is
        # accepted, not from when its request completes - see settings.py.
        limit_concurrency=settings.limit_concurrency,
        # Bounds memory for a single connection stuck mid-header (this
        # process's one available mitigation for that case without adding
        # new infrastructure - settings.py's own docstring has the full
        # reasoning and residual gap).
        h11_max_incomplete_event_size=settings.h11_max_incomplete_event_size,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
