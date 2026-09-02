"""Read access to the ``llm_calls`` journal, for the ``/debug/runs`` page.

Separate from ``PostgresLLMJournal`` (``tc_infrastructure.llm.journal``), which
is write-oriented and whose narrow read methods (``replay_map``, ``calls_for``,
``usage_for``) exist for offline replay and usage aggregation, not for listing
recent calls across a workspace - the same write/read split
``thought_reader.py`` documents for ``thoughts``.

Every query is workspace-scoped. The workspace comes from authentication,
never from an untrusted parameter (docs/DESIGN.md 10).
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
from tc_infrastructure.db.tables import llm_calls

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25


@dataclass(frozen=True, slots=True)
class LlmCallRecord:
    """One journaled call, summary fields only.

    Deliberately omits ``request_messages`` and ``response_raw``: those carry
    raw prompt/completion content (personal memory text), and this is a
    read-only inspection surface for call *metadata* - which model was asked,
    what was requested of it, what it cost - not a way to read captured
    content a second time. ``request_params`` is included: it is
    request-shape metadata (temperature, routing, reasoning control), not
    thought content.
    """

    id: uuid.UUID
    run_id: uuid.UUID
    step: str
    sequence: int
    model_requested: str
    model_served: str | None
    provider: str | None
    request_params: dict[str, Any]
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: Decimal | None
    latency_ms: int | None
    error_code: str | None
    created_at: dt.datetime


@dataclass(frozen=True, slots=True)
class LlmCallPage:
    items: tuple[LlmCallRecord, ...]
    next_cursor: str | None


class PostgresLlmCallReader:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_llm_calls(
        self,
        workspace_id: WorkspaceId,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> LlmCallPage:
        """Newest first, keyset-paginated on ``(created_at, id)``.

        ``id`` alone cannot be the keyset column the way ``thoughts.id`` is:
        it is a random UUID, not a chronological sequence. ``created_at`` is
        the real ordering key; ``id`` breaks ties when two calls in the same
        run journal at the same timestamp, so no row is silently skipped or
        repeated across a page boundary.
        """
        limit = max(1, min(limit, MAX_PAGE_SIZE))

        conditions = [llm_calls.c.workspace_id == workspace_id]
        if cursor is not None:
            created_at, call_id = _decode_cursor(cursor)
            conditions.append(
                sa.tuple_(llm_calls.c.created_at, llm_calls.c.id) < (created_at, call_id)
            )

        # One extra row tells us whether another page exists without a count.
        statement = (
            sa.select(llm_calls)
            .where(*conditions)
            .order_by(llm_calls.c.created_at.desc(), llm_calls.c.id.desc())
            .limit(limit + 1)
        )

        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()

        has_more = len(rows) > limit
        rows = rows[:limit]
        items = tuple(_to_record(row) for row in rows)
        next_cursor = (
            _encode_cursor(items[-1].created_at, items[-1].id) if has_more and items else None
        )
        return LlmCallPage(items=items, next_cursor=next_cursor)


def _to_record(row: sa.Row[Any]) -> LlmCallRecord:
    return LlmCallRecord(
        id=row.id,
        run_id=row.run_id,
        step=row.step,
        sequence=row.sequence,
        model_requested=row.model_requested,
        model_served=row.model_served,
        provider=row.provider,
        request_params=row.request_params or {},
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        estimated_cost_usd=row.estimated_cost_usd,
        latency_ms=row.latency_ms,
        error_code=row.error_code,
        created_at=row.created_at,
    )


def _encode_cursor(created_at: dt.datetime, call_id: uuid.UUID) -> str:
    """Opaque to callers, so the pagination key can change without breaking them."""
    payload = f"{int(created_at.timestamp() * 1_000_000)}:{call_id}"
    return uuid.uuid5(uuid.NAMESPACE_OID, payload).hex[:8] + f".{payload}"


def _decode_cursor(cursor: str) -> tuple[dt.datetime, uuid.UUID]:
    try:
        _, payload = cursor.split(".", 1)
        micros_str, call_id_str = payload.split(":", 1)
        created_at = dt.datetime.fromtimestamp(int(micros_str) / 1_000_000, tz=dt.UTC)
        return created_at, uuid.UUID(call_id_str)
    except (IndexError, ValueError) as exc:
        raise InvalidCursorError(f"{cursor!r} is not a valid pagination cursor") from exc


class InvalidCursorError(ValueError):
    """The supplied pagination cursor could not be interpreted."""
