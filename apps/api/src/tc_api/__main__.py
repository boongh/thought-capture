"""API entrypoint.

    python -m tc_api

Binds to the configured host, which defaults to loopback. Nothing in Release 1
is meant to be reachable off-host (docs/DESIGN.md 12.2).
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from tc_infrastructure.config import get_settings
from tc_infrastructure.runtime import event_loop_factory


class MissingBearerTokenError(RuntimeError):
    """The API refuses to start without the local bearer token."""


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = get_settings()

    # Fail here rather than in Compose interpolation: a required variable in the
    # compose file blocks every service, including the database. Failing in the
    # service that needs the token keeps the rest of the stack startable, and
    # failing at startup beats serving an API that 401s every request.
    if not settings.api_bearer_token.get_secret_value():
        raise MissingBearerTokenError(
            "TC_API_BEARER_TOKEN must be set. Generate one with: "
            'python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )

    # uvicorn builds its own loop; psycopg cannot use Windows' default one, so
    # the same factory the rest of the system uses is installed here too.
    server = uvicorn.Server(
        uvicorn.Config(
            "tc_api.app:create_app",
            factory=True,
            host=settings.api_host,
            port=settings.api_port,
            log_level="info",
            access_log=True,
        )
    )
    loop = event_loop_factory()()
    try:
        loop.run_until_complete(server.serve())
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
