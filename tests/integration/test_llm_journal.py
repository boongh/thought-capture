"""The LLM call journal (ADR-0008).

The journal is what turns "switch models" from a leap into a measurable
decision. Its guarantees: every attempt is recorded, nothing can be rewritten,
and a recorded run can be replayed without calling a provider.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_application.structured import complete_structured
from tc_domain.capture import WorkspaceId
from tc_domain.llm import LLMRequest, LLMResponse, LLMStep, Message
from tc_infrastructure.db.llm_journal import PostgresLLMJournal
from tc_infrastructure.db.tables import llm_calls, runs
from tc_infrastructure.llm.offline import OfflineLLMProvider, request_fingerprint

pytestmark = pytest.mark.integration


class Thing(BaseModel):
    value: str


VALID = json.dumps({"value": "ok"})


@pytest.fixture
def workspace(seeded_identity: tuple[uuid.UUID, uuid.UUID]) -> WorkspaceId:
    return WorkspaceId(seeded_identity[0])


@pytest.fixture
def journal(app_session_factory: async_sessionmaker[AsyncSession]) -> PostgresLLMJournal:
    return PostgresLLMJournal(app_session_factory)


async def a_run(factory: async_sessionmaker[AsyncSession], workspace_id: WorkspaceId) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            sa.insert(runs).values(
                id=run_id, workspace_id=workspace_id, kind="organize", status="running"
            )
        )
    return run_id


def a_request(content: str = "organize these") -> LLMRequest:
    return LLMRequest(
        step=LLMStep.ORGANIZE,
        messages=(Message(role="user", content=content),),
        schema_name="Thing",
        json_schema={"type": "object"},
        prompt_version="v1",
        schema_version="v1",
    )


def a_response(content: str = VALID) -> LLMResponse:
    return LLMResponse(
        content=content,
        raw={"content": content},
        latency_ms=42,
        model_requested="synthetic/model",
        model_served="synthetic/model-0.1",
        provider="synthetic",
        generation_id="gen-123",
        input_tokens=100,
        output_tokens=20,
        cost_usd=Decimal("0.000123"),
        request_params={"temperature": 0.0},
    )


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


async def test_a_call_is_recorded_with_what_was_sent_and_returned(
    journal: PostgresLLMJournal,
    workspace: WorkspaceId,
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await a_run(app_session_factory, workspace)

    await journal.record(
        workspace_id=workspace,
        run_id=run_id,
        request=a_request(),
        response=a_response(),
        sequence=1,
    )

    async with app_session_factory() as session:
        row = (
            await session.execute(sa.select(llm_calls).where(llm_calls.c.run_id == run_id))
        ).one()

    assert row.step == "organize"
    assert row.prompt_version == "v1"
    assert row.model_requested == "synthetic/model"
    # Recorded rather than assumed: a gateway may route elsewhere.
    assert row.model_served == "synthetic/model-0.1"
    assert row.generation_id == "gen-123"
    assert row.input_tokens == 100
    assert row.latency_ms == 42
    assert row.request_messages[0]["content"] == "organize these"


async def test_the_same_step_and_sequence_cannot_be_recorded_twice(
    journal: PostgresLLMJournal,
    workspace: WorkspaceId,
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The uniqueness rule still holds when a caller pins both values."""
    run_id = await a_run(app_session_factory, workspace)
    await journal.record(
        workspace_id=workspace,
        run_id=run_id,
        request=a_request(),
        response=a_response(),
        sequence=1,
        ordinal=1,
    )

    with pytest.raises(sa.exc.IntegrityError):
        await journal.record(
            workspace_id=workspace,
            run_id=run_id,
            request=a_request(),
            response=a_response(),
            sequence=1,
            ordinal=1,
        )


async def test_two_repaired_stages_in_one_run_do_not_collide(
    journal: PostgresLLMJournal,
    workspace: WorkspaceId,
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every stage's repair is attempt 2, so a naive (step, attempt) key clashes.

    Select and organize each needing one repair would both try to record
    ``(run, 'repair', 2)``. The run-global ordinal is what keeps them apart.
    """
    run_id = await a_run(app_session_factory, workspace)
    repair = LLMRequest(
        step=LLMStep.REPAIR,
        messages=(Message(role="user", content="fix it"),),
        schema_name="Thing",
        json_schema={"type": "object"},
        prompt_version="v1",
        schema_version="v1",
    )

    # Two stages, each recording its own repair, with no ordinal supplied.
    await journal.record(
        workspace_id=workspace, run_id=run_id, request=repair, response=a_response(), sequence=2
    )
    await journal.record(
        workspace_id=workspace, run_id=run_id, request=repair, response=a_response(), sequence=2
    )

    calls = await journal.calls_for(workspace, run_id)
    assert len(calls) == 2
    assert [c.ordinal for c in calls] == [1, 2]


async def test_the_application_role_cannot_rewrite_the_journal(
    journal: PostgresLLMJournal,
    workspace: WorkspaceId,
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Layer one: the running system holds no UPDATE grant on the journal."""
    run_id = await a_run(app_session_factory, workspace)
    await journal.record(
        workspace_id=workspace,
        run_id=run_id,
        request=a_request(),
        response=a_response(),
        sequence=1,
    )

    async with app_session_factory() as session, session.begin():
        with pytest.raises(sa.exc.ProgrammingError, match="permission denied"):
            await session.execute(
                sa.update(llm_calls)
                .where(llm_calls.c.run_id == run_id)
                .values(model_requested="something-else")
            )


async def test_not_even_the_schema_owner_can_rewrite_the_journal(
    journal: PostgresLLMJournal,
    workspace: WorkspaceId,
    app_session_factory: async_sessionmaker[AsyncSession],
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Layer two: the trigger, which stops what grants cannot.

    The schema owner *does* hold UPDATE, so without the trigger a migration or
    an admin session could quietly rewrite history. A journal that can be
    edited cannot reproduce anything (ADR-0008).
    """
    run_id = await a_run(app_session_factory, workspace)
    await journal.record(
        workspace_id=workspace,
        run_id=run_id,
        request=a_request(),
        response=a_response(),
        sequence=1,
    )

    async with admin_session_factory() as session, session.begin():
        with pytest.raises(sa.exc.DBAPIError, match="append-only"):
            await session.execute(
                sa.update(llm_calls)
                .where(llm_calls.c.run_id == run_id)
                .values(model_requested="something-else")
            )


async def test_the_journal_cannot_be_deleted(
    journal: PostgresLLMJournal,
    workspace: WorkspaceId,
    app_session_factory: async_sessionmaker[AsyncSession],
    admin_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await a_run(app_session_factory, workspace)
    await journal.record(
        workspace_id=workspace,
        run_id=run_id,
        request=a_request(),
        response=a_response(),
        sequence=1,
    )

    async with admin_session_factory() as session, session.begin():
        with pytest.raises(sa.exc.DBAPIError, match="append-only"):
            await session.execute(sa.delete(llm_calls).where(llm_calls.c.run_id == run_id))


async def test_usage_sums_every_attempt(
    journal: PostgresLLMJournal,
    workspace: WorkspaceId,
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Including the failed one, which was still paid for."""
    run_id = await a_run(app_session_factory, workspace)
    await journal.record(
        workspace_id=workspace,
        run_id=run_id,
        request=a_request(),
        response=a_response(),
        sequence=1,
    )
    await journal.record(
        workspace_id=workspace,
        run_id=run_id,
        request=a_request("second"),
        response=a_response(),
        sequence=2,
    )

    assert await journal.usage_for(workspace, run_id) == (200, 40)


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


async def test_a_journaled_run_replays_without_calling_a_provider(
    journal: PostgresLLMJournal,
    workspace: WorkspaceId,
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The deterministic half of ADR-0008, end to end.

    Record a run, build an offline provider from the journal, and re-derive the
    same output. The provider raises rather than inventing, so a replay that
    misses would fail rather than quietly produce something new.
    """
    run_id = await a_run(app_session_factory, workspace)
    request = a_request("the original question")

    live = OfflineLLMProvider(responder=lambda _: VALID)
    first = await complete_structured(
        live,
        request,
        Thing,
        journal=lambda req, resp, attempt: journal.record(
            workspace_id=workspace, run_id=run_id, request=req, response=resp, sequence=attempt
        ),
    )

    recorded = await journal.replay_map(workspace, run_id)
    assert request_fingerprint(request) in recorded

    replayed_provider = OfflineLLMProvider(recorded=recorded)
    replayed = await complete_structured(replayed_provider, request, Thing)

    assert replayed.value == first.value
    assert replayed.attempts == 1


async def test_replaying_a_question_that_was_never_asked_fails(
    journal: PostgresLLMJournal,
    workspace: WorkspaceId,
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A replay must not silently answer a question the run never asked."""
    run_id = await a_run(app_session_factory, workspace)
    await journal.record(
        workspace_id=workspace,
        run_id=run_id,
        request=a_request("asked"),
        response=a_response(),
        sequence=1,
    )

    provider = OfflineLLMProvider(recorded=await journal.replay_map(workspace, run_id))

    from tc_domain.llm import LLMError

    with pytest.raises(LLMError):
        await provider.complete(a_request("never asked"))


async def test_calls_for_a_run_are_returned_in_order(
    journal: PostgresLLMJournal,
    workspace: WorkspaceId,
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await a_run(app_session_factory, workspace)
    for sequence in (1, 2, 3):
        await journal.record(
            workspace_id=workspace,
            run_id=run_id,
            request=a_request(f"call {sequence}"),
            response=a_response(),
            sequence=sequence,
        )

    calls = await journal.calls_for(workspace, run_id)

    assert [c.sequence for c in calls] == [1, 2, 3]
