"""``Search``'s semantic and hybrid modes (docs/DESIGN.md 7.5, docs/adr/0010)."""

from __future__ import annotations

import datetime as dt
import uuid

from tc_application.search import Search
from tc_domain.capture import WorkspaceId
from tc_domain.khoj_export import khoj_filename
from tc_domain.khoj_ports import KhojSearchResult, KhojUnavailableError
from tc_domain.search import SearchPage, SearchQuery, SearchResult
from tests.unit.fakes import FakeExactSearch, FakeKhoj

WORKSPACE = WorkspaceId(uuid.uuid4())
OTHER_WORKSPACE = WorkspaceId(uuid.uuid4())


def _result(
    *, document_id: uuid.UUID | None = None, revision_id: uuid.UUID | None = None
) -> SearchResult:
    return SearchResult(
        result_id="r1",
        document_id=document_id or uuid.uuid4(),
        revision_id=revision_id or uuid.uuid4(),
        thought_ids=(),
        kind="project",
        title="A project",
        snippet="…",
        updated_at=dt.datetime.now(dt.UTC),
        entities=(),
        channels=("exact",),
        rank=1.0,
    )


def _hit(workspace_id: WorkspaceId, document_id: uuid.UUID, *, score: float) -> KhojSearchResult:
    filename = khoj_filename(
        workspace_id=workspace_id, kind="project", stable_key="project:x", document_id=document_id
    )
    return KhojSearchResult(entry="…", score=score, filename=filename)


async def test_semantic_hydrates_khoj_hits_from_postgres() -> None:
    document_id = uuid.uuid4()
    hydrated_result = _result(document_id=document_id)
    khoj = FakeKhoj(search_results=(_hit(WORKSPACE, document_id, score=0.9),))
    exact = FakeExactSearch(hydrated={document_id: hydrated_result})
    search = Search(exact, khoj)

    page = await search(WORKSPACE, SearchQuery(q="hello"), mode="semantic")

    assert page.degraded is False
    assert len(page.items) == 1
    assert page.items[0].document_id == document_id
    assert page.items[0].channels == ("semantic",)
    assert page.items[0].rank == 0.9


async def test_semantic_drops_hits_for_another_workspace() -> None:
    document_id = uuid.uuid4()
    khoj = FakeKhoj(search_results=(_hit(OTHER_WORKSPACE, document_id, score=0.9),))
    exact = FakeExactSearch(hydrated={document_id: _result(document_id=document_id)})
    search = Search(exact, khoj)

    page = await search(WORKSPACE, SearchQuery(q="hello"), mode="semantic")

    assert page.items == ()
    assert page.degraded is False


async def test_semantic_drops_a_hit_with_no_current_revision() -> None:
    """Indexed in Khoj but no longer PostgreSQL's current revision (deleted,
    or superseded since the last sync) - PostgreSQL stays authoritative
    (docs/DESIGN.md 8.2)."""
    document_id = uuid.uuid4()
    khoj = FakeKhoj(search_results=(_hit(WORKSPACE, document_id, score=0.9),))
    exact = FakeExactSearch(hydrated={})  # nothing hydrates
    search = Search(exact, khoj)

    page = await search(WORKSPACE, SearchQuery(q="hello"), mode="semantic")

    assert page.items == ()


async def test_semantic_dedupes_multiple_khoj_entries_for_the_same_document() -> None:
    """Khoj can chunk one uploaded Markdown file into more than one indexed
    entry and return several hits sharing the same filename (independent
    review finding, docs/adr/0010) - the same document must not appear twice
    in a semantic page."""
    document_id = uuid.uuid4()
    khoj = FakeKhoj(
        search_results=(
            _hit(WORKSPACE, document_id, score=0.95),
            _hit(WORKSPACE, document_id, score=0.80),
        )
    )
    exact = FakeExactSearch(hydrated={document_id: _result(document_id=document_id)})
    search = Search(exact, khoj)

    page = await search(WORKSPACE, SearchQuery(q="hello"), mode="semantic")

    assert len(page.items) == 1
    assert page.items[0].rank == 0.95  # the first (best-ranked) occurrence wins


async def test_semantic_degrades_when_khoj_is_unavailable() -> None:
    khoj = FakeKhoj(raises=KhojUnavailableError("down"))
    exact = FakeExactSearch()
    search = Search(exact, khoj)

    page = await search(WORKSPACE, SearchQuery(q="hello"), mode="semantic")

    assert page.degraded is True
    assert page.items == ()


async def test_semantic_with_no_query_text_is_an_empty_non_degraded_answer() -> None:
    khoj = FakeKhoj()
    exact = FakeExactSearch()
    search = Search(exact, khoj)

    page = await search(WORKSPACE, SearchQuery(), mode="semantic")

    assert page.degraded is False
    assert page.items == ()
    assert khoj.search_calls == []


async def test_hybrid_fuses_exact_and_semantic_by_rank_position() -> None:
    shared_document_id = uuid.uuid4()
    shared_revision_id = uuid.uuid4()
    exact_only = _result()
    shared_from_exact = _result(document_id=shared_document_id, revision_id=shared_revision_id)

    exact = FakeExactSearch(
        SearchPage(items=(shared_from_exact, exact_only), next_cursor=None),
        hydrated={shared_document_id: shared_from_exact},
    )
    khoj = FakeKhoj(search_results=(_hit(WORKSPACE, shared_document_id, score=0.99),))
    search = Search(exact, khoj)

    page = await search(WORKSPACE, SearchQuery(q="hello"), mode="hybrid")

    assert page.degraded is False
    by_revision = {item.revision_id: item for item in page.items}
    # Present in both channels: ranks first (RRF score is additive across
    # channels) and its `channels` reflects both.
    assert page.items[0].revision_id == shared_revision_id
    assert set(by_revision[shared_revision_id].channels) == {"exact", "semantic"}
    assert by_revision[exact_only.revision_id].channels == ("exact",)


async def test_hybrid_does_not_inflate_score_or_mislabel_channels_for_a_chunked_khoj_document() -> (
    None
):
    """A document Khoj chunked into two entries (independent review finding,
    docs/adr/0010) - found only by the semantic channel - must count once in
    RRF fusion, not twice, and must not be labeled as also found by the exact
    channel just because it appeared twice in Khoj's own results."""
    semantic_only_document_id = uuid.uuid4()
    other_exact_result = _result()  # occupies exact rank 1, unrelated document

    exact = FakeExactSearch(
        SearchPage(items=(other_exact_result,), next_cursor=None),
        hydrated={semantic_only_document_id: _result(document_id=semantic_only_document_id)},
    )
    khoj = FakeKhoj(
        search_results=(
            _hit(WORKSPACE, semantic_only_document_id, score=0.9),
            _hit(WORKSPACE, semantic_only_document_id, score=0.7),
        )
    )
    search = Search(exact, khoj)

    page = await search(WORKSPACE, SearchQuery(q="hello"), mode="hybrid")

    matches = [item for item in page.items if item.document_id == semantic_only_document_id]
    assert len(matches) == 1
    assert matches[0].channels == ("semantic",)
    # Exactly one RRF term (rank 1 in the semantic channel), not two.
    assert matches[0].rank == 1.0 / 61


async def test_hybrid_phrase_boost_outranks_a_non_phrase_semantic_hit() -> None:
    phrase_document_id = uuid.uuid4()
    semantic_only_document_id = uuid.uuid4()
    phrase_result = _result(document_id=phrase_document_id)
    semantic_result = _result(document_id=semantic_only_document_id)

    exact = FakeExactSearch(
        SearchPage(items=(phrase_result,), next_cursor=None),
        hydrated={semantic_only_document_id: semantic_result},
    )
    # The semantic hit ranks *first* from Khoj's own ordering, but the exact
    # phrase match must still outrank it once the boost applies.
    khoj = FakeKhoj(search_results=(_hit(WORKSPACE, semantic_only_document_id, score=0.99),))
    search = Search(exact, khoj)

    page = await search(WORKSPACE, SearchQuery(phrase="exact words"), mode="hybrid")

    assert page.items[0].document_id == phrase_document_id


async def test_hybrid_degrades_but_keeps_exact_results_when_khoj_fails() -> None:
    exact = FakeExactSearch(SearchPage(items=(_result(),), next_cursor=None))
    khoj = FakeKhoj(raises=KhojUnavailableError("down"))
    search = Search(exact, khoj)

    page = await search(WORKSPACE, SearchQuery(q="hello"), mode="hybrid")

    assert page.degraded is True
    assert len(page.items) == 1


async def test_hybrid_and_semantic_never_return_a_next_cursor() -> None:
    """RRF fusion needs the whole result set to rank correctly, which does
    not compose with exact search's simple keyset cursor (docs/adr/0010) -
    documented as unimplemented this slice, not silently wrong."""
    exact = FakeExactSearch(SearchPage(items=(_result(),), next_cursor="would-paginate"))
    khoj = FakeKhoj()
    search = Search(exact, khoj)

    hybrid_page = await search(WORKSPACE, SearchQuery(q="hello"), mode="hybrid")
    semantic_page = await search(WORKSPACE, SearchQuery(q="hello"), mode="semantic")

    assert hybrid_page.next_cursor is None
    assert semantic_page.next_cursor is None
