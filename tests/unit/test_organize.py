"""The organize pipeline's orchestration, driven with fakes (docs/DESIGN.md 7.2)."""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest

from tc_application.organize import OrganizeWindow
from tc_domain.capture import ThoughtId, WorkspaceId
from tc_domain.context import Tier1Row
from tc_domain.llm import LLMError, LLMRequest, LLMResponse, LLMStep
from tc_domain.organize import OrganizeCoverageError, WindowThought, WindowTooLargeError
from tc_domain.windows import CaptureWindow
from tc_infrastructure.llm.offline import OfflineLLMProvider
from tests.unit.fakes import (
    FakeContextIndex,
    FakeOrganizeWriter,
    FakeRunLedger,
    FakeThoughtWindowReader,
)

WORKSPACE = WorkspaceId(uuid.uuid4())
WINDOW = CaptureWindow(
    start=dt.datetime(2026, 8, 30, 13, tzinfo=dt.UTC),
    end=dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC),
)


def journal_factory(workspace_id: WorkspaceId, run_id: uuid.UUID):
    async def write(request: LLMRequest, response: LLMResponse, attempt: int) -> None:
        return None

    return write


async def no_usage(workspace_id: WorkspaceId, run_id: uuid.UUID) -> tuple[int, int]:
    return 0, 0


def make_pipeline(
    *,
    thoughts: list[WindowThought],
    provider: object,
    select_provider: object | None = None,
    context_index: FakeContextIndex | None = None,
    writer: FakeOrganizeWriter | None = None,
    run_ledger: FakeRunLedger | None = None,
    max_window_tokens: int | None = None,
) -> tuple[OrganizeWindow, FakeOrganizeWriter, FakeRunLedger]:
    writer = writer or FakeOrganizeWriter()
    run_ledger = run_ledger or FakeRunLedger()
    kwargs: dict[str, object] = {}
    if max_window_tokens is not None:
        kwargs["max_window_tokens"] = max_window_tokens
    pipeline = OrganizeWindow(
        thoughts=FakeThoughtWindowReader(thoughts),
        context_index=context_index or FakeContextIndex(),
        provider=provider,  # type: ignore[arg-type]
        select_provider=select_provider,  # type: ignore[arg-type]
        journal_factory=journal_factory,
        writer=writer,
        run_ledger=run_ledger,
        usage_for=no_usage,
        clock=lambda: dt.datetime(2026, 8, 31, 13, tzinfo=dt.UTC),
        **kwargs,  # type: ignore[arg-type]
    )
    return pipeline, writer, run_ledger


def a_thought(id_: int, body: str = "went for a walk") -> WindowThought:
    return WindowThought(
        id=ThoughtId(id_), body=body, client_local_date="2026-08-31", client_local_time="10:00:00"
    )


async def test_an_empty_window_skips_the_llm_and_writer() -> None:
    provider = OfflineLLMProvider()
    pipeline, writer, run_ledger = make_pipeline(thoughts=[], provider=provider)

    run_id = await pipeline(WORKSPACE, WINDOW)

    assert provider.calls == []
    assert writer.calls == []
    assert len(run_ledger.succeeded) == 1
    assert run_ledger.succeeded[0]["run_id"] == run_id
    assert run_ledger.succeeded[0]["input_tokens"] == 0


async def test_the_happy_path_writes_and_marks_the_run_succeeded() -> None:
    organize_reply = json.dumps(
        {
            "documents": [
                {
                    "stable_key": "daily_digest:2026-08-31",
                    "kind": "daily_digest",
                    "title": "Daily digest",
                    "body_markdown": "## Summary\n\nWalked.\n\n## Current state\n\n-\n\n## Open threads\n\n-\n\n## Timeline\n\n- walked",
                    "source_thought_ids": [1],
                    "mentioned_entities": [],
                    "change_summary": "first digest",
                    "confidence": 1.0,
                }
            ],
            "unorganized_thought_ids": [],
            "referenced_document_keys": [],
        }
    )
    provider = OfflineLLMProvider(responder=lambda _req: organize_reply)
    pipeline, writer, run_ledger = make_pipeline(thoughts=[a_thought(1)], provider=provider)

    await pipeline(WORKSPACE, WINDOW)

    assert len(writer.calls) == 1
    # The digest's stable_key is forced to the window-end timestamp
    # (docs/DESIGN.md 8.3), never whatever the model proposed - see
    # `_to_write_request`.
    assert writer.calls[0].documents[0].stable_key == WINDOW.end.isoformat()
    # Marking the run succeeded now happens inside `writer.write` itself
    # (atomic with everything else it wrote), not as a separate
    # `run_ledger.succeed` call - see `RunOutcome`.
    assert len(writer.outcomes) == 1
    assert run_ledger.succeeded == []
    assert run_ledger.failed == []


async def test_the_select_call_uses_the_dedicated_select_provider() -> None:
    """organize and select are independently pinned models (docs/DESIGN.md 11):
    the select call must go to `select_provider`, never `provider`, when the
    two differ.
    """
    organize_reply = json.dumps(
        {
            "documents": [
                {
                    "stable_key": "daily_digest:2026-08-31",
                    "kind": "daily_digest",
                    "title": "Daily digest",
                    "body_markdown": "## Summary\n\nWalked.\n\n## Current state\n\n-\n\n## Open threads\n\n-\n\n## Timeline\n\n- walked",
                    "source_thought_ids": [1],
                    "mentioned_entities": [],
                    "change_summary": "first digest",
                    "confidence": 1.0,
                }
            ],
            "unorganized_thought_ids": [],
            "referenced_document_keys": [],
        }
    )
    select_reply = json.dumps({"stable_keys": [], "reasoning": ""})
    organize_provider = OfflineLLMProvider(
        responder=lambda _req: organize_reply, model_id="offline/organize"
    )
    select_provider = OfflineLLMProvider(
        responder=lambda _req: select_reply, model_id="offline/select"
    )
    index = (
        Tier1Row(
            stable_key="project:aurora",
            entity_type="project",
            canonical_name="Aurora",
            aliases=(),
            summary="",
            last_mentioned_at=None,
            open_thread_count=0,
        ),
    )
    pipeline, _writer, _run_ledger = make_pipeline(
        thoughts=[a_thought(1)],
        provider=organize_provider,
        select_provider=select_provider,
        context_index=FakeContextIndex(index=index),
    )

    await pipeline(WORKSPACE, WINDOW)

    assert [r.step for r in select_provider.calls] == [LLMStep.SELECT]
    assert [r.step for r in organize_provider.calls] == [LLMStep.ORGANIZE]


async def test_the_select_call_falls_back_to_the_organize_provider_when_unset() -> None:
    """No select_provider given -> both calls share `provider`, matching every
    pre-existing call site that only ever passed one provider."""
    organize_reply = json.dumps(
        {
            "documents": [
                {
                    "stable_key": "daily_digest:2026-08-31",
                    "kind": "daily_digest",
                    "title": "Daily digest",
                    "body_markdown": "## Summary\n\nWalked.\n\n## Current state\n\n-\n\n## Open threads\n\n-\n\n## Timeline\n\n- walked",
                    "source_thought_ids": [1],
                    "mentioned_entities": [],
                    "change_summary": "first digest",
                    "confidence": 1.0,
                }
            ],
            "unorganized_thought_ids": [],
            "referenced_document_keys": [],
        }
    )
    select_reply = json.dumps({"stable_keys": [], "reasoning": ""})

    def responder(request: LLMRequest) -> str:
        return select_reply if request.step == LLMStep.SELECT else organize_reply

    provider = OfflineLLMProvider(responder=responder)
    index = (
        Tier1Row(
            stable_key="project:aurora",
            entity_type="project",
            canonical_name="Aurora",
            aliases=(),
            summary="",
            last_mentioned_at=None,
            open_thread_count=0,
        ),
    )
    pipeline, _writer, _run_ledger = make_pipeline(
        thoughts=[a_thought(1)],
        provider=provider,
        context_index=FakeContextIndex(index=index),
    )

    await pipeline(WORKSPACE, WINDOW)

    assert {r.step for r in provider.calls} == {LLMStep.SELECT, LLMStep.ORGANIZE}


async def test_incomplete_coverage_fails_the_run_and_raises() -> None:
    """A digest citing only one of two window thoughts leaves the other
    uncovered - a schema-valid reply (satisfying the "exactly one digest"
    contract) that still fails the separate coverage check.
    """
    organize_reply = json.dumps(
        {
            "documents": [
                {
                    "stable_key": "daily_digest:2026-08-31",
                    "kind": "daily_digest",
                    "title": "Daily digest",
                    "body_markdown": "## Summary\n\nWalked.\n\n## Current state\n\n-\n\n## Open threads\n\n-\n\n## Timeline\n\n- walked",
                    "source_thought_ids": [1],
                    "mentioned_entities": [],
                    "change_summary": "first digest",
                    "confidence": 1.0,
                }
            ],
            "unorganized_thought_ids": [],
            "referenced_document_keys": [],
        }
    )
    provider = OfflineLLMProvider(responder=lambda _req: organize_reply)
    pipeline, writer, run_ledger = make_pipeline(
        thoughts=[a_thought(1), a_thought(2)], provider=provider
    )

    with pytest.raises(OrganizeCoverageError):
        await pipeline(WORKSPACE, WINDOW)

    assert writer.calls == []
    assert len(run_ledger.failed) == 1
    assert run_ledger.failed[0]["error_code"] == "OrganizeCoverageError"
    assert run_ledger.succeeded == []


async def test_a_provider_failure_on_organize_fails_the_run_and_raises() -> None:
    class FailingProvider:
        model_id = "failing/model"
        supports_strict_schema = False

        async def complete(self, request: LLMRequest) -> LLMResponse:
            raise LLMError("synthetic outage")

    pipeline, writer, run_ledger = make_pipeline(
        thoughts=[a_thought(1)], provider=FailingProvider()
    )

    with pytest.raises(LLMError):
        await pipeline(WORKSPACE, WINDOW)

    assert writer.calls == []
    assert len(run_ledger.failed) == 1
    assert run_ledger.failed[0]["error_code"] == "LLMError"


async def test_an_oversized_window_fails_before_touching_the_provider_or_writer() -> None:
    """docs/DESIGN.md 14.1: a very large window must not reach the provider
    unbounded. Real chunking-with-overlap is a follow-up; this is the safety
    bound in the meantime - it must fail fast, before context assembly or
    the organize call, which is why a failing provider proves it: if the
    bound did not trip first, this test would raise ``LLMError`` instead.
    """

    class ExplodingProvider:
        model_id = "should-not-be-called/model"
        supports_strict_schema = False

        async def complete(self, request: LLMRequest) -> LLMResponse:
            raise AssertionError("the provider must not be called for an oversized window")

    long_thought = a_thought(1, body="x" * 100_000)
    pipeline, writer, run_ledger = make_pipeline(
        thoughts=[long_thought], provider=ExplodingProvider(), max_window_tokens=100
    )

    with pytest.raises(WindowTooLargeError):
        await pipeline(WORKSPACE, WINDOW)

    assert writer.calls == []
    assert len(run_ledger.failed) == 1
    assert run_ledger.failed[0]["error_code"] == "WindowTooLargeError"
    assert run_ledger.succeeded == []
