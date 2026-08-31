"""The append-only journal of model calls (ADR-0008).

Every attempt is recorded, including the one that failed validation, because
reproduction depends on knowing what was actually sent and returned - not on a
tidied summary of the successful path.

The rows contain raw personal content: organize prompts embed thought bodies
verbatim. This table is as sensitive as ``thoughts`` and must never be logged
or exported.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import WorkspaceId
from tc_domain.llm import LLMRequest, LLMResponse, LLMStep
from tc_infrastructure.db.tables import llm_calls
from tc_infrastructure.llm.offline import request_fingerprint

logger = logging.getLogger(__name__)

# Where the request fingerprint is stashed inside request_params, so a replay
# does not have to reconstruct the original request to find its answer.
FINGERPRINT_KEY = "_fingerprint"


@dataclass(frozen=True, slots=True)
class JournaledCall:
    step: LLMStep
    sequence: int
    content: str
    model_served: str | None
    generation_id: str | None


class PostgresLLMJournal:
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
    ) -> uuid.UUID:
        """Append one call. Unique on ``(run_id, step, sequence)``."""
        call_id = uuid.uuid4()
        async with self._session_factory() as session, session.begin():
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
                    request_params={
                        **response.request_params,
                        FINGERPRINT_KEY: request_fingerprint(request),
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
                "sequence": sequence,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
            },
        )
        return call_id

    async def replay_map(self, run_id: uuid.UUID) -> dict[str, str]:
        """Recorded replies for a run, keyed by request fingerprint.

        Feeds ``OfflineLLMProvider`` so a rebuild re-derives documents from the
        exact bytes the provider returned, without calling it again.

        The fingerprint is read back from what was stored rather than
        recomputed. Recomputing would require reconstructing the original
        request exactly, and any drift in that reconstruction would silently
        produce a cache miss instead of a replay.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(llm_calls.c.request_params, llm_calls.c.response_raw)
                    .where(llm_calls.c.run_id == run_id)
                    .order_by(llm_calls.c.sequence)
                )
            ).all()

        recorded: dict[str, str] = {}
        for row in rows:
            content = _content_of(row.response_raw)
            fingerprint = (row.request_params or {}).get(FINGERPRINT_KEY)
            if content is not None and isinstance(fingerprint, str):
                recorded[fingerprint] = content
        return recorded

    async def calls_for(self, run_id: uuid.UUID) -> list[JournaledCall]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    sa.select(
                        llm_calls.c.step,
                        llm_calls.c.sequence,
                        llm_calls.c.model_served,
                        llm_calls.c.generation_id,
                        llm_calls.c.response_raw,
                    )
                    .where(llm_calls.c.run_id == run_id)
                    .order_by(llm_calls.c.sequence)
                )
            ).all()

        return [
            JournaledCall(
                step=LLMStep(row.step),
                sequence=row.sequence,
                content=_content_of(row.response_raw) or "",
                model_served=row.model_served,
                generation_id=row.generation_id,
            )
            for row in rows
        ]

    async def usage_for(self, run_id: uuid.UUID) -> tuple[int, int]:
        """Total input and output tokens across every attempt in a run."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    sa.select(
                        sa.func.coalesce(sa.func.sum(llm_calls.c.input_tokens), 0),
                        sa.func.coalesce(sa.func.sum(llm_calls.c.output_tokens), 0),
                    ).where(llm_calls.c.run_id == run_id)
                )
            ).one()
        return int(row[0]), int(row[1])


def _content_of(raw: dict[str, object] | None) -> str | None:
    """Pull the assistant text out of a stored provider response.

    Handles both shapes: an OpenAI-compatible completion, and the flat form the
    offline provider records.
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
