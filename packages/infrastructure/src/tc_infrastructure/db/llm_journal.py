"""The append-only journal of model calls (ADR-0008).

Every attempt is recorded, including the one that failed validation and the one
that never reached the provider, because reproduction depends on knowing what
was actually sent and what came back - not on a tidied summary of the happy
path.

The rows contain raw personal content: organize prompts embed thought bodies
verbatim. This table is as sensitive as ``thoughts`` and must never be logged
or exported. Every method is workspace-scoped for the same reason.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import WorkspaceId
from tc_domain.llm import LLMRequest, LLMResponse, LLMStep
from tc_infrastructure.db.tables import llm_calls
from tc_infrastructure.llm.offline import request_fingerprint

logger = logging.getLogger(__name__)


def _ordinal_lock_key(workspace_id: WorkspaceId, run_id: uuid.UUID) -> int:
    """A stable 63-bit advisory-lock key for one run's ordinal allocation.

    Same construction as ``windows.advisory_key``: a distinct namespace prefix
    keeps this from ever colliding with the window lock's keys.
    """
    material = f"llm_journal_ordinal:{workspace_id}:{run_id}"
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


# Stashed inside request_params so a replay does not have to reconstruct the
# original request to find its answer.
FINGERPRINT_KEY = "_fingerprint"
# A run-global ordinal. `sequence` alone is not enough: several stages in one
# run each start at 1, and two stages both needing repair would otherwise
# collide on (run_id, 'repair', 2).
ORDINAL_KEY = "_ordinal"


@dataclass(frozen=True, slots=True)
class JournaledCall:
    step: LLMStep
    sequence: int
    ordinal: int
    content: str
    model_served: str | None
    generation_id: str | None
    error_code: str | None


class WorkspaceMismatchError(RuntimeError):
    """A journal row was requested for a run that belongs to another workspace."""


class PostgresLLMJournal:
    """Workspace-scoped, append-only record of every model call."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        *,
        workspace_id: WorkspaceId,
        run_id: uuid.UUID,
        request: LLMRequest,
        response: LLMResponse,
        sequence: int,
        ordinal: int | None = None,
    ) -> uuid.UUID:
        """Append one call.

        ``sequence`` is unique per ``(run_id, step)``; ``ordinal`` orders the
        whole run. When ``ordinal`` is omitted it is allocated, and ``sequence``
        falls back to it, so two stages that both need a repair cannot collide.

        Allocation and insert happen inside one transaction, serialised by a
        run-scoped advisory lock: counting existing rows in one transaction and
        inserting in a later one (as this used to do) lets two concurrent
        callers for the same run both count zero and both allocate ordinal 1,
        producing nondeterministic replay order and, when ``sequence`` also
        matches, a collision on the unique constraint instead of a clean retry.
        """
        call_id = uuid.uuid4()
        async with self._session_factory() as session, session.begin():
            if ordinal is None:
                await session.execute(
                    sa.select(
                        sa.func.pg_advisory_xact_lock(_ordinal_lock_key(workspace_id, run_id))
                    )
                )
                used = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(llm_calls)
                    .where(
                        llm_calls.c.workspace_id == workspace_id,
                        llm_calls.c.run_id == run_id,
                    )
                )
                ordinal = int(used or 0) + 1
                sequence = ordinal

            await session.execute(
                sa.insert(llm_calls).values(
                    id=call_id,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    step=str(request.step),
                    sequence=sequence,
                    prompt_version=request.prompt_version,
                    schema_version=request.schema_version,
                    model_requested=response.model_requested,
                    model_served=response.model_served,
                    provider=response.provider,
                    generation_id=response.generation_id,
                    request_messages=[
                        {"role": m.role, "content": m.content} for m in request.messages
                    ],
                    # The complete effective request, so ADR-0008 can audit and
                    # reconstruct exactly what was asked - not just a one-way
                    # hash of it.
                    request_params={
                        **response.request_params,
                        "schema_name": request.schema_name,
                        "json_schema": request.json_schema,
                        "max_output_tokens": request.max_output_tokens,
                        "temperature": request.temperature,
                        FINGERPRINT_KEY: request_fingerprint(request),
                        ORDINAL_KEY: ordinal,
                    },
                    response_raw=response.raw,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    estimated_cost_usd=response.cost_usd,
                    latency_ms=response.latency_ms,
                    error_code=response.error_code,
                )
            )
        # Identifiers and shapes only; never the prompt or the reply.
        logger.info(
            "llm.journaled",
            extra={
                "run_id": str(run_id),
                "step": str(request.step),
                "ordinal": ordinal,
                "error_code": response.error_code,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
            },
        )
        return call_id

    async def replay_map(
        self, workspace_id: WorkspaceId, run_id: uuid.UUID
    ) -> dict[str, list[str]]:
        """Recorded replies for a run, keyed by fingerprint, **in order**.

        A list rather than a single value: if the same request was issued twice
        in one run and the provider answered differently, collapsing them would
        make exact replay impossible. The offline provider consumes these in
        order, so a repeated question replays its own second answer.

        The fingerprint is read back from what was stored rather than
        recomputed; recomputing would require reconstructing the request
        exactly, and any drift would silently produce a cache miss.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(llm_calls.c.request_params, llm_calls.c.response_raw)
                    .where(
                        llm_calls.c.workspace_id == workspace_id,
                        llm_calls.c.run_id == run_id,
                    )
                    .order_by(_ordinal_of(llm_calls.c.request_params), llm_calls.c.sequence)
                )
            ).all()

        recorded: dict[str, list[str]] = {}
        for row in rows:
            content = _content_of(row.response_raw)
            fingerprint = (row.request_params or {}).get(FINGERPRINT_KEY)
            if content is not None and isinstance(fingerprint, str):
                recorded.setdefault(fingerprint, []).append(content)
        return recorded

    async def calls_for(self, workspace_id: WorkspaceId, run_id: uuid.UUID) -> list[JournaledCall]:
        """Every call in a run, in the order it was issued."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(
                        llm_calls.c.step,
                        llm_calls.c.sequence,
                        llm_calls.c.model_served,
                        llm_calls.c.generation_id,
                        llm_calls.c.error_code,
                        llm_calls.c.request_params,
                        llm_calls.c.response_raw,
                    )
                    .where(
                        llm_calls.c.workspace_id == workspace_id,
                        llm_calls.c.run_id == run_id,
                    )
                    .order_by(_ordinal_of(llm_calls.c.request_params), llm_calls.c.sequence)
                )
            ).all()

        return [
            JournaledCall(
                step=LLMStep(row.step),
                sequence=row.sequence,
                ordinal=int((row.request_params or {}).get(ORDINAL_KEY, row.sequence)),
                content=_content_of(row.response_raw) or "",
                model_served=row.model_served,
                generation_id=row.generation_id,
                error_code=row.error_code,
            )
            for row in rows
        ]

    async def usage_for(self, workspace_id: WorkspaceId, run_id: uuid.UUID) -> tuple[int, int]:
        """Total input and output tokens across every attempt in a run."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    sa.select(
                        sa.func.coalesce(sa.func.sum(llm_calls.c.input_tokens), 0),
                        sa.func.coalesce(sa.func.sum(llm_calls.c.output_tokens), 0),
                    ).where(
                        llm_calls.c.workspace_id == workspace_id,
                        llm_calls.c.run_id == run_id,
                    )
                )
            ).one()
        return int(row[0]), int(row[1])


def _ordinal_of(column: Any) -> Any:
    """Order by the stored run-global ordinal.

    Read out of the JSONB payload rather than given its own column, so that no
    migration is needed for a value that only orders diagnostics.
    """
    return sa.cast(column[ORDINAL_KEY].astext, sa.Integer())


def _content_of(raw: dict[str, object] | None) -> str | None:
    """Pull the assistant text out of a stored provider response.

    Handles both shapes: an OpenAI-compatible completion, and the flat form the
    offline provider records. Returns None for a failure row, which has no
    content by definition.
    """
    if not raw:
        return None

    flat = raw.get("content")
    if isinstance(flat, str):
        return flat

    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None
