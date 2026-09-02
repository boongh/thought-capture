"""The ``runs`` row lifecycle for the organize pipeline (docs/DESIGN.md 6.3).

Split into its own small transactions rather than folded into
``PostgresOrganizeWriter``'s big one: the run row must exist *before* the
first LLM call, whose journal entry references it (ADR-0008), and it must
still be updatable to ``failed`` after a validation or provider error - a
point at which the big write transaction never opened at all.
"""

from __future__ import annotations

import datetime as dt
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import WorkspaceId
from tc_infrastructure.db.tables import runs


class PostgresRunLedger:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def start(
        self, workspace_id: WorkspaceId, *, window_start: dt.datetime, window_end: dt.datetime
    ) -> uuid.UUID:
        run_id = uuid.uuid4()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                sa.insert(runs).values(
                    id=run_id,
                    workspace_id=workspace_id,
                    kind="organize",
                    status="running",
                    window_start=window_start,
                    window_end=window_end,
                    started_at=sa.func.now(),
                )
            )
        return run_id

    async def succeed(
        self,
        run_id: uuid.UUID,
        *,
        model_provider: str | None,
        model_id: str | None,
        prompt_version: str,
        input_tokens: int,
        output_tokens: int,
        context_recall: float | None,
        context_degraded: bool,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                sa.update(runs)
                .where(runs.c.id == run_id)
                .values(
                    status="succeeded",
                    finished_at=sa.func.now(),
                    model_provider=model_provider,
                    model_id=model_id,
                    prompt_version=prompt_version,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    context_recall=context_recall,
                    context_degraded=context_degraded,
                )
            )

    async def fail(self, run_id: uuid.UUID, *, error_code: str, error_detail: str) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                sa.update(runs)
                .where(runs.c.id == run_id)
                .values(
                    status="failed",
                    finished_at=sa.func.now(),
                    error_code=error_code,
                    error_detail=error_detail,
                )
            )
