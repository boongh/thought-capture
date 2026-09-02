"""Read access to the ``runs`` row (docs/DESIGN.md 6.3), for ``/status``.

Separate from ``PostgresRunLedger`` (``tc_infrastructure.db.run_ledger``),
which is write-oriented and used only by the organize pipeline itself - the
same write/read split ``llm_call_reader.py`` documents for ``llm_calls``.

Every query is workspace-scoped. The workspace comes from authentication (the
resolved Discord identity), never from an untrusted parameter.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import WorkspaceId
from tc_infrastructure.db.tables import runs


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One ``runs`` row: status and outcome metadata, never prompt content."""

    id: uuid.UUID
    kind: str
    status: str
    window_start: dt.datetime | None
    window_end: dt.datetime | None
    prompt_version: str | None
    model_provider: str | None
    model_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: Decimal | None
    context_recall: Decimal | None
    context_degraded: bool
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    error_code: str | None
    error_detail: str | None
    created_at: dt.datetime


class PostgresRunReader:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, workspace_id: WorkspaceId, run_id: uuid.UUID) -> RunRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    sa.select(runs).where(runs.c.workspace_id == workspace_id, runs.c.id == run_id)
                )
            ).first()
        return _to_record(row) if row is not None else None

    async def most_recent(self, workspace_id: WorkspaceId) -> RunRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    sa.select(runs)
                    .where(runs.c.workspace_id == workspace_id)
                    .order_by(runs.c.created_at.desc())
                    .limit(1)
                )
            ).first()
        return _to_record(row) if row is not None else None


def _to_record(row: sa.Row[Any]) -> RunRecord:
    return RunRecord(
        id=row.id,
        kind=row.kind,
        status=row.status,
        window_start=row.window_start,
        window_end=row.window_end,
        prompt_version=row.prompt_version,
        model_provider=row.model_provider,
        model_id=row.model_id,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        estimated_cost_usd=row.estimated_cost_usd,
        context_recall=row.context_recall,
        context_degraded=row.context_degraded,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error_code=row.error_code,
        error_detail=row.error_detail,
        created_at=row.created_at,
    )
