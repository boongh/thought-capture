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
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
