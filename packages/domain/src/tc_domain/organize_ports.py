"""Ports the organize pipeline depends on, implemented by ``tc_infrastructure``.

Split from ``tc_domain.ports`` (capture's ports) because these are a
different use case's dependencies, not because the rule is different: every
signature here is stdlib-only, so ``tc_application``'s orchestrator never has
to import SQLAlchemy or Pydantic to depend on them (docs/DESIGN.md 5.3).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Protocol, runtime_checkable

from tc_domain.capture import WorkspaceId
from tc_domain.context import Tier1Row
from tc_domain.organize import OrganizeWriteRequest, OrganizeWriteResult, WindowThought


@runtime_checkable
class ThoughtWindowReader(Protocol):
    async def list_between(
        self, workspace_id: WorkspaceId, start: dt.datetime, end: dt.datetime
    ) -> list[WindowThought]:
        """Every thought whose ``client_created_at`` falls in ``(start, end]``.

        Stable order (docs/DESIGN.md 7.2 step 2: "load raw thoughts in stable
        timestamp/ID order"), so two runs over the same committed window
        build the same prompt.
        """
        ...


@runtime_checkable
class ContextIndexPort(Protocol):
    async def tier1_index(self, workspace_id: WorkspaceId) -> tuple[Tier1Row, ...]:
        """The complete entity-document index (docs/DESIGN.md 7.3.1)."""
        ...

    async def bodies_for(
        self, workspace_id: WorkspaceId, stable_keys: frozenset[str]
    ) -> dict[str, str]:
        """Full current ``body_markdown`` for the given, already-selected keys."""
        ...


@runtime_checkable
class OrganizeWriter(Protocol):
    async def write(
        self, *, workspace_id: WorkspaceId, run_id: uuid.UUID, request: OrganizeWriteRequest
    ) -> OrganizeWriteResult:
        """Write every document revision, provenance row, entity mention,
        context-selection record, and the digest outbox event in one
        transaction (docs/DESIGN.md 7.2 step 9: never write a partial
        document set after validation failure - so this either commits
        everything or raises and commits nothing).
        """
        ...


@runtime_checkable
class RunLedger(Protocol):
    async def start(
        self, workspace_id: WorkspaceId, *, window_start: dt.datetime, window_end: dt.datetime
    ) -> uuid.UUID:
        """Create the ``runs`` row before anything else - including the first
        LLM call, whose journal entry needs a valid ``run_id`` to reference.
        """
        ...

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
    ) -> None: ...

    async def fail(self, run_id: uuid.UUID, *, error_code: str, error_detail: str) -> None:
        """``error_detail`` must already be sanitized - identifiers and shapes
        only, never raw thought text or a provider error body
        (docs/DESIGN.md 14.2).
        """
        ...
