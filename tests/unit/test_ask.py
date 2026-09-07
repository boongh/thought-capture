"""``AskQuestion`` (docs/DESIGN.md 7.6, docs/adr/0003's "Ask proxy" amendment)."""

from __future__ import annotations

import datetime as dt
import uuid

from tc_application.ask import AskQuestion, collect_ask_answer
from tc_domain.capture import WorkspaceId
from tc_domain.khoj_ports import KhojChatChunk, KhojChatReference, KhojUnavailableError
from tc_domain.search import SearchPage, SearchQuery, SearchResult
from tests.unit.fakes import FakeExactSearch, FakeKhojPort, FakeSemanticHydrator

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


def _ask(
    khoj: FakeKhojPort | None = None,
    hydrate: FakeSemanticHydrator | None = None,
    exact: FakeExactSearch | None = None,
    *,
    enabled: bool = True,
) -> AskQuestion:
    return AskQuestion(
        khoj or FakeKhojPort(),
        hydrate or FakeSemanticHydrator(),
        exact or FakeExactSearch(),
        enabled=enabled,
    )


async def test_disabled_ask_makes_no_khoj_call() -> None:
    khoj = FakeKhojPort()
    ask = _ask(khoj, enabled=False)

    answer = await collect_ask_answer(ask(WORKSPACE, SearchQuery(q="what happened last week?")))

    assert answer.enabled is False
    assert answer.degraded is False
    assert answer.strict_unsupported is False
    assert answer.answer is None
    assert answer.references == ()
    assert khoj.chat_calls == []


async def test_enabled_ask_streams_and_collects_the_answer() -> None:
    khoj = FakeKhojPort(
        chat_chunks=(
            KhojChatChunk(text_delta="It went "),
            KhojChatChunk(text_delta="well."),
            KhojChatChunk(done=True),
        )
    )
    ask = _ask(khoj)

    chunks = [c async for c in ask(WORKSPACE, SearchQuery(q="how did the launch go?"))]
    answer = await collect_ask_answer(ask(WORKSPACE, SearchQuery(q="how did the launch go?")))

    # Real streaming, not one buffered chunk: the text arrives in the pieces
    # Khoj sent it in, not pre-joined.
    assert [c.text_delta for c in chunks if c.text_delta] == ["It went ", "well."]
    assert chunks[-1].done is True
    assert answer.enabled is True
    assert answer.degraded is False
    assert answer.answer == "It went well."
    assert khoj.chat_calls == [("how did the launch go?", 5), ("how did the launch go?", 5)]


async def test_khoj_unavailable_degrades_rather_than_erroring() -> None:
    khoj = FakeKhojPort(raises=KhojUnavailableError("down"))
    ask = _ask(khoj)

    answer = await collect_ask_answer(ask(WORKSPACE, SearchQuery(q="anything?")))

    assert answer.enabled is True
    assert answer.degraded is True
    assert answer.answer is None
    assert answer.references == ()


async def test_a_partial_answer_before_failure_is_kept_and_flagged_degraded() -> None:
    """A stream that yields some text and then fails must not look like a
    silently-complete answer (docs/DESIGN.md 7.5)."""

    async def failing_chat(question: str, *, limit: int = 5):  # type: ignore[no-untyped-def]
        yield KhojChatChunk(text_delta="Partial answer")
        raise KhojUnavailableError("connection dropped")

    khoj = FakeKhojPort()
    khoj.chat = failing_chat  # type: ignore[method-assign]
    ask = _ask(khoj)

    answer = await collect_ask_answer(ask(WORKSPACE, SearchQuery(q="q")))

    assert answer.answer == "Partial answer"
    assert answer.degraded is True


async def test_references_are_resolved_via_the_semantic_hydrator() -> None:
    document_id = uuid.uuid4()
    khoj = FakeKhojPort(
        chat_chunks=(
            KhojChatChunk(
                references=(
                    KhojChatReference(compiled="…", filename="ws/project/x--doc.md", heading="H"),
                )
            ),
            KhojChatChunk(text_delta="answer"),
            KhojChatChunk(done=True),
        )
    )
    hydrate = FakeSemanticHydrator({"ws/project/x--doc.md": _result(document_id)})
    ask = _ask(khoj, hydrate)

    answer = await collect_ask_answer(ask(WORKSPACE, SearchQuery(q="q")))

    assert len(answer.references) == 1
    assert answer.references[0].document_id == document_id
    assert hydrate.calls[0][2] == ("ws/project/x--doc.md",)


async def test_a_reference_the_hydrator_does_not_resolve_is_dropped() -> None:
    """Unparseable, another workspace's, filtered out, or stale - the
    hydrator's own contract - is simply absent, never a dangling citation."""
    khoj = FakeKhojPort(
        chat_chunks=(
            KhojChatChunk(
                references=(
                    KhojChatReference(compiled="…", filename="not/resolved.md", heading="H"),
                )
            ),
            KhojChatChunk(text_delta="answer"),
            KhojChatChunk(done=True),
        )
    )
    ask = _ask(khoj)

    answer = await collect_ask_answer(ask(WORKSPACE, SearchQuery(q="q")))

    assert answer.answer == "answer"
    assert answer.references == ()


async def test_duplicate_references_to_the_same_chunked_document_are_deduped() -> None:
    """Khoj can cite more than one chunk of the same uploaded document in one
    answer - the answer must not list the same document twice."""
    document_id = uuid.uuid4()
    khoj = FakeKhojPort(
        chat_chunks=(
            KhojChatChunk(
                references=(
                    KhojChatReference(
                        compiled="…chunk one…", filename="ws/project/x--doc.md", heading="A"
                    ),
                    KhojChatReference(
                        compiled="…chunk two…", filename="ws/project/x--doc.md", heading="B"
                    ),
                )
            ),
            KhojChatChunk(done=True),
        )
    )
    hydrate = FakeSemanticHydrator({"ws/project/x--doc.md": _result(document_id)})
    ask = _ask(khoj, hydrate)

    answer = await collect_ask_answer(ask(WORKSPACE, SearchQuery(q="q")))

    assert len(answer.references) == 1
    assert answer.references[0].document_id == document_id
    # Deduped before hydrating too - one filename requested, not two.
    assert hydrate.calls[0][2] == ("ws/project/x--doc.md",)


async def test_no_references_needs_no_hydrator_call() -> None:
    khoj = FakeKhojPort(chat_chunks=(KhojChatChunk(text_delta="answer"), KhojChatChunk(done=True)))
    hydrate = FakeSemanticHydrator()
    ask = _ask(khoj, hydrate)

    await collect_ask_answer(ask(WORKSPACE, SearchQuery(q="q")))

    assert hydrate.calls == []


# ---------------------------------------------------------------------------
# Strict filters + capability fallback (docs/DESIGN.md 7.6)
# ---------------------------------------------------------------------------


async def test_a_bare_question_never_triggers_the_strict_fallback() -> None:
    khoj = FakeKhojPort(chat_chunks=(KhojChatChunk(text_delta="answer"), KhojChatChunk(done=True)))
    exact = FakeExactSearch()
    ask = _ask(khoj, exact=exact)

    answer = await collect_ask_answer(ask(WORKSPACE, SearchQuery(q="q")))

    assert answer.strict_unsupported is False
    assert exact.calls == []
    assert khoj.chat_calls == [("q", 5)]


async def test_a_structured_filter_triggers_the_exact_search_fallback_with_no_khoj_call() -> None:
    fallback_page = SearchPage(items=(), next_cursor=None)
    khoj = FakeKhojPort(chat_chunks=(KhojChatChunk(text_delta="should never be sent"),))
    exact = FakeExactSearch(fallback_page)
    ask = _ask(khoj, exact=exact)
    query = SearchQuery(q="q", kind="project")

    answer = await collect_ask_answer(ask(WORKSPACE, query))

    assert answer.strict_unsupported is True
    assert answer.answer is None
    assert answer.fallback is fallback_page
    assert exact.calls == [(WORKSPACE, query)]
    assert khoj.chat_calls == []  # no LLM call made at all


async def test_each_filter_field_independently_triggers_strict_mode() -> None:
    for query in (
        SearchQuery(q="q", phrase="exact phrase"),
        SearchQuery(q="q", include=("must",)),
        SearchQuery(q="q", exclude=("not",)),
        SearchQuery(q="q", entity_id=uuid.uuid4()),
        SearchQuery(q="q", source="discord"),
        SearchQuery(q="q", date_from=dt.date(2026, 1, 1)),
        SearchQuery(q="q", date_to=dt.date(2026, 1, 1)),
        SearchQuery(q="q", local_time_from=dt.time(9, 0)),
        SearchQuery(q="q", local_time_to=dt.time(9, 0)),
    ):
        khoj = FakeKhojPort(chat_chunks=(KhojChatChunk(done=True),))
        ask = _ask(khoj)
        answer = await collect_ask_answer(ask(WORKSPACE, query))
        assert answer.strict_unsupported is True, query
        assert khoj.chat_calls == []
