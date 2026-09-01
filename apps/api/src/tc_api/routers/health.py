"""Liveness and readiness.

Deliberately distinct: liveness answers "is this process running", readiness
answers "can it serve traffic". Conflating them makes an orchestrator restart a
healthy process because a dependency is briefly unavailable (docs/DESIGN.md 10).

Neither endpoint is authenticated: they carry no personal data, and requiring a
token would make them useless to a container health check.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from fastapi import APIRouter

from tc_api.dependencies import Context
from tc_api.problems import service_unavailable
from tc_api.schemas import HealthResponse, ReadinessResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=HealthResponse, summary="Process liveness only")
async def live() -> HealthResponse:
    """Never touches a dependency, by design."""
    return HealthResponse(status="ok")


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Database and required dependency readiness",
)
async def ready(context: Context) -> ReadinessResponse:
    try:
        async with context.session_factory() as session:
            await session.execute(sa.text("SELECT 1"))
        pending = await context.outbox.pending_count()
    except Exception as exc:
        logger.warning("health.not_ready", extra={"error_class": type(exc).__name__})
        raise service_unavailable("the canonical database is not reachable") from exc

    return ReadinessResponse(status="ok", database="ok", pending_outbox_events=pending)
