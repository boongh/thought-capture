"""``AskQuestion`` (docs/DESIGN.md 7.6, docs/adr/0003's "Ask proxy" amendment)."""

from __future__ import annotations

import datetime as dt
import uuid

from tc_application.ask import AskQuestion
from tc_domain.capture import WorkspaceId
from tc_domain.khoj_ports import KhojChatReference, KhojChatResult, KhojUnavailableError
from tc_domain.search import SearchResult
from tests.unit.fakes import FakeKhojPort, FakeSemanticHydrator

WORKSPACE = WorkspaceId(uuid.uuid4())


def _result(document_id: uuid.UUID) -> SearchResult:
    return SearchResult(
        result_id="r1",
        document_id=document_id,
        revision_id=uuid.uuid4(),
        thought_ids=(),
        kind="project",
        title="A project",
        snippet="…",
        updated_at=dt.datetime.now(dt.UTC),
        entities=(),
        channels=("semantic",),
        rank=0.0,
    )


async def test_disabled_ask_makes_no_khoj_call() -> None:
    khoj = FakeKhojPort()
    ask = AskQuestion(khoj, FakeSemanticHydrator(), enabled=False)

    answer = await ask(WORKSPACE, "what happened last week?")

    assert answer.enabled is False
    assert answer.degraded is False
    assert answer.answer is None
    assert answer.references == ()
    assert khoj.chat_calls == []


async def test_enabled_ask_returns_the_answer() -> None:
    khoj = FakeKhojPort(chat_result=KhojChatResult(response="It went well.", references=()))
    ask = AskQuestion(khoj, FakeSemanticHydrator(), enabled=True)

    answer = await ask(WORKSPACE, "how did the launch go?")

    assert answer.enabled is True
    assert answer.degraded is False
    assert answer.answer == "It went well."
    assert khoj.chat_calls == [("how did the launch go?", 5)]


async def test_khoj_unavailable_degrades_rather_than_erroring() -> None:
    khoj = FakeKhojPort(raises=KhojUnavailableError("down"))
    ask = AskQuestion(khoj, FakeSemanticHydrator(), enabled=True)

    answer = await ask(WORKSPACE, "anything?")

    assert answer.enabled is True
    assert answer.degraded is True
    assert answer.answer is None
    assert answer.references == ()


async def test_references_are_resolved_via_the_semantic_hydrator() -> None:
    document_id = uuid.uuid4()
    khoj = FakeKhojPort(
        chat_result=KhojChatResult(
            response="answer",
            references=(
                KhojChatReference(compiled="…", filename="ws/project/x--doc.md", heading="H"),
            ),
        )
    )
    hydrate = FakeSemanticHydrator({"ws/project/x--doc.md": _result(document_id)})
    ask = AskQuestion(khoj, hydrate, enabled=True)

    answer = await ask(WORKSPACE, "q")

    assert len(answer.references) == 1
    assert answer.references[0].document_id == document_id
    assert hydrate.calls[0][2] == ("ws/project/x--doc.md",)


async def test_a_reference_the_hydrator_does_not_resolve_is_dropped() -> None:
    """Unparseable, another workspace's, filtered out, or stale - the
    hydrator's own contract - is simply absent, never a dangling citation."""
    khoj = FakeKhojPort(
        chat_result=KhojChatResult(
            response="answer",
            references=(KhojChatReference(compiled="…", filename="not/resolved.md", heading="H"),),
        )
    )
    ask = AskQuestion(khoj, FakeSemanticHydrator(), enabled=True)

    answer = await ask(WORKSPACE, "q")

    assert answer.answer == "answer"
    assert answer.references == ()


async def test_duplicate_references_to_the_same_chunked_document_are_deduped() -> None:
    """Khoj can cite more than one chunk of the same uploaded document in one
    answer - the answer must not list the same document twice."""
    document_id = uuid.uuid4()
    khoj = FakeKhojPort(
        chat_result=KhojChatResult(
            response="answer",
            references=(
                KhojChatReference(
                    compiled="…chunk one…", filename="ws/project/x--doc.md", heading="A"
                ),
                KhojChatReference(
                    compiled="…chunk two…", filename="ws/project/x--doc.md", heading="B"
                ),
            ),
        )
    )
    hydrate = FakeSemanticHydrator({"ws/project/x--doc.md": _result(document_id)})
    ask = AskQuestion(khoj, hydrate, enabled=True)

    answer = await ask(WORKSPACE, "q")

    assert len(answer.references) == 1
    assert answer.references[0].document_id == document_id
    # Deduped before hydrating too - one filename requested, not two.
    assert hydrate.calls[0][2] == ("ws/project/x--doc.md",)


async def test_no_references_needs_no_hydrator_call() -> None:
    khoj = FakeKhojPort(chat_result=KhojChatResult(response="answer", references=()))
    hydrate = FakeSemanticHydrator()
    ask = AskQuestion(khoj, hydrate, enabled=True)

    await ask(WORKSPACE, "q")

    assert hydrate.calls == []
