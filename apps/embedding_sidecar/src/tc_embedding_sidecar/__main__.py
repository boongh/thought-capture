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
        # docstring has the full reasoning, including a correction: this
        # does not cap or close a *stalled* connection, only affects
        # whether an *other, complete* request gets 503 once enough are
        # open) - beyond this many open connections, a complete request
        # arriving on one more gets 503 from uvicorn itself, before this
        # process's own code - including MaxBodySizeMiddleware's body
        # buffering - ever runs for it.
        limit_concurrency=settings.limit_concurrency,
        # Bounds memory for a single connection stuck mid-header (this
        # process's one available mitigation for that case without adding
        # new infrastructure - settings.py's own docstring has the full
        # reasoning and residual gap: this bounds neither how many such
        # connections may be held nor for how long).
        h11_max_incomplete_event_size=settings.h11_max_incomplete_event_size,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
