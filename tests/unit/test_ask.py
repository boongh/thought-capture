"""``AskQuestion`` (docs/DESIGN.md 7.6, docs/adr/0010)."""

from __future__ import annotations

import datetime as dt
import uuid

from tc_application.ask import AskQuestion
from tc_domain.capture import WorkspaceId
from tc_domain.khoj_export import khoj_filename
from tc_domain.khoj_ports import KhojChatReference, KhojChatResult, KhojUnavailableError
from tc_domain.search import SearchResult
from tests.unit.fakes import FakeExactSearch, FakeKhoj

WORKSPACE = WorkspaceId(uuid.uuid4())
OTHER_WORKSPACE = WorkspaceId(uuid.uuid4())


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
        channels=(),
        rank=0.0,
    )


async def test_disabled_ask_makes_no_khoj_call() -> None:
    khoj = FakeKhoj()
    ask = AskQuestion(khoj, FakeExactSearch(), enabled=False)

    answer = await ask(WORKSPACE, "what happened last week?")

    assert answer.enabled is False
    assert answer.degraded is False
    assert answer.answer is None
    assert answer.references == ()
    assert khoj.chat_calls == []


async def test_enabled_ask_prefixes_notes_mode_and_returns_the_answer() -> None:
    khoj = FakeKhoj(chat_result=KhojChatResult(response="It went well.", references=()))
    ask = AskQuestion(khoj, FakeExactSearch(), enabled=True)

    answer = await ask(WORKSPACE, "how did the launch go?")

    assert answer.enabled is True
    assert answer.degraded is False
    assert answer.answer == "It went well."
    assert khoj.chat_calls == [("how did the launch go?", 5)]


async def test_khoj_unavailable_degrades_rather_than_erroring() -> None:
    khoj = FakeKhoj(raises=KhojUnavailableError("down"))
    ask = AskQuestion(khoj, FakeExactSearch(), enabled=True)

    answer = await ask(WORKSPACE, "anything?")

    assert answer.enabled is True
    assert answer.degraded is True
    assert answer.answer is None
    assert answer.references == ()


async def test_references_are_resolved_and_workspace_verified() -> None:
    document_id = uuid.uuid4()
    other_document_id = uuid.uuid4()
    good_filename = khoj_filename(
        workspace_id=WORKSPACE, kind="project", stable_key="project:x", document_id=document_id
    )
    other_workspace_filename = khoj_filename(
        workspace_id=OTHER_WORKSPACE,
        kind="project",
        stable_key="project:y",
        document_id=other_document_id,
    )
    khoj = FakeKhoj(
        chat_result=KhojChatResult(
            response="answer",
            references=(
                KhojChatReference(compiled="…", filename=good_filename, heading="H"),
                KhojChatReference(compiled="…", filename=other_workspace_filename, heading="H"),
                KhojChatReference(compiled="…", filename="not/a/real-file.md", heading="H"),
            ),
        )
    )
    exact = FakeExactSearch(hydrated={document_id: _result(document_id)})
    ask = AskQuestion(khoj, exact, enabled=True)

    answer = await ask(WORKSPACE, "q")

    assert len(answer.references) == 1
    assert answer.references[0].document_id == document_id


async def test_duplicate_references_to_the_same_chunked_document_are_deduped() -> None:
    """Khoj can cite more than one chunk of the same uploaded document in one
    answer (independent review finding, docs/adr/0010) - the answer must not
    list the same document twice."""
    document_id = uuid.uuid4()
    filename = khoj_filename(
        workspace_id=WORKSPACE, kind="project", stable_key="project:x", document_id=document_id
    )
    khoj = FakeKhoj(
        chat_result=KhojChatResult(
            response="answer",
            references=(
                KhojChatReference(compiled="…chunk one…", filename=filename, heading="A"),
                KhojChatReference(compiled="…chunk two…", filename=filename, heading="B"),
            ),
        )
    )
    exact = FakeExactSearch(hydrated={document_id: _result(document_id)})
    ask = AskQuestion(khoj, exact, enabled=True)

    answer = await ask(WORKSPACE, "q")

    assert len(answer.references) == 1
    assert answer.references[0].document_id == document_id


async def test_a_reference_with_no_current_revision_is_dropped_not_shown_dangling() -> None:
    document_id = uuid.uuid4()
    filename = khoj_filename(
        workspace_id=WORKSPACE, kind="project", stable_key="project:x", document_id=document_id
    )
    khoj = FakeKhoj(
        chat_result=KhojChatResult(
            response="answer",
            references=(KhojChatReference(compiled="…", filename=filename, heading="H"),),
        )
    )
    exact = FakeExactSearch(hydrated={})
    ask = AskQuestion(khoj, exact, enabled=True)

    answer = await ask(WORKSPACE, "q")

    assert answer.answer == "answer"
    assert answer.references == ()
