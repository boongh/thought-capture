"""``Search`` use-case mode-gating, semantic hydration, and hybrid degradation
(docs/DESIGN.md 7.5, 10)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from tc_application.search import Search, UnsupportedSearchModeError
from tc_domain.capture import WorkspaceId
from tc_domain.khoj_ports import KhojSearchResult, KhojUnavailableError
from tc_domain.search import SearchPage, SearchQuery, SearchResult
from tests.unit.fakes import FakeExactSearch, FakeKhojPort, FakeSemanticHydrator

WORKSPACE = WorkspaceId(uuid.uuid4())


def _result(
    *, channels: tuple[str, ...] = ("exact",), rank: float = 1.0, **overrides: object
) -> SearchResult:
    defaults: dict[str, object] = {
        "result_id": str(uuid.uuid4()),
        "document_id": uuid.uuid4(),
        "revision_id": uuid.uuid4(),
        "thought_ids": (),
        "kind": "project",
        "title": "A project",
        "snippet": "…",
        "updated_at": dt.datetime.now(dt.UTC),
        "entities": (),
        "channels": channels,
        "rank": rank,
    }
    defaults.update(overrides)
    return SearchResult(**defaults)  # type: ignore[arg-type]


def _search(
    *,
    exact: FakeExactSearch | None = None,
    khoj: FakeKhojPort | None = None,
    hydrate: FakeSemanticHydrator | None = None,
) -> Search:
    return Search(
        exact or FakeExactSearch(), khoj or FakeKhojPort(), hydrate or FakeSemanticHydrator()
    )


# ---------------------------------------------------------------------------
# Mode gating
# ---------------------------------------------------------------------------


async def test_exact_mode_delegates_to_the_exact_port() -> None:
    page = SearchPage(items=(_result(),), next_cursor=None)
    exact = FakeExactSearch(page)
    search = _search(exact=exact)

    result = await search(WORKSPACE, SearchQuery(q="hello"), mode="exact")

    assert result is page
    assert exact.calls == [(WORKSPACE, SearchQuery(q="hello"))]


async def test_default_mode_is_exact() -> None:
    exact = FakeExactSearch()
    search = _search(exact=exact)

    await search(WORKSPACE, SearchQuery())

    assert len(exact.calls) == 1


async def test_an_unknown_mode_raises() -> None:
    search = _search()

    with pytest.raises(UnsupportedSearchModeError):
        await search(WORKSPACE, SearchQuery(), mode="quantum")


# ---------------------------------------------------------------------------
# Semantic mode
# ---------------------------------------------------------------------------


async def test_semantic_mode_hydrates_khoj_results_and_carries_their_score() -> None:
    hydrated = _result(channels=("semantic",))
    khoj = FakeKhojPort(
        search_results=(KhojSearchResult(entry="...", score=0.42, filename="ws/project/x.md"),)
    )
    hydrate = FakeSemanticHydrator({"ws/project/x.md": hydrated})
    search = _search(khoj=khoj, hydrate=hydrate)

    page = await search(WORKSPACE, SearchQuery(q="aurora"), mode="semantic")

    assert page.degraded is False
    assert len(page.items) == 1
    assert page.items[0].revision_id == hydrated.revision_id
    assert page.items[0].rank == pytest.approx(0.42)
    assert khoj.search_queries == ["aurora"]


async def test_semantic_mode_drops_a_result_that_does_not_hydrate() -> None:
    khoj = FakeKhojPort(
        search_results=(KhojSearchResult(entry="...", score=0.9, filename="unresolvable.md"),)
    )
    search = _search(khoj=khoj, hydrate=FakeSemanticHydrator({}))

    page = await search(WORKSPACE, SearchQuery(q="aurora"), mode="semantic")

    assert page.items == ()
    assert page.degraded is False


async def test_semantic_mode_with_no_free_text_is_an_empty_non_degraded_answer() -> None:
    """A pure filter/phrase query has nothing to send Khoj - this is a valid
    empty result, not a degradation, and Khoj must not even be called."""
    khoj = FakeKhojPort()
    search = _search(khoj=khoj)

    page = await search(WORKSPACE, SearchQuery(kind="project"), mode="semantic")

    assert page.items == ()
    assert page.degraded is False
    assert khoj.search_queries == []


async def test_semantic_mode_degrades_explicitly_when_khoj_is_unavailable() -> None:
    khoj = FakeKhojPort(raises=KhojUnavailableError("connection refused"))
    search = _search(khoj=khoj)

    page = await search(WORKSPACE, SearchQuery(q="aurora"), mode="semantic")

    assert page.items == ()
    assert page.degraded is True


# ---------------------------------------------------------------------------
# Hybrid mode
# ---------------------------------------------------------------------------


async def test_hybrid_mode_fuses_exact_and_semantic_results() -> None:
    shared_revision = uuid.uuid4()
    exact_only = _result(channels=("exact",))
    both = _result(channels=("exact",), revision_id=shared_revision)
    semantic_hydrated = _result(channels=("semantic",), revision_id=shared_revision)

    exact = FakeExactSearch(SearchPage(items=(both, exact_only), next_cursor=None))
    khoj = FakeKhojPort(
        search_results=(KhojSearchResult(entry="...", score=0.9, filename="ws/project/x.md"),)
    )
    hydrate = FakeSemanticHydrator({"ws/project/x.md": semantic_hydrated})
    search = _search(exact=exact, khoj=khoj, hydrate=hydrate)

    page = await search(WORKSPACE, SearchQuery(q="aurora"), mode="hybrid")

    assert page.degraded is False
    by_revision = {r.revision_id: r for r in page.items}
    assert set(by_revision) == {both.revision_id, exact_only.revision_id}
    assert by_revision[shared_revision].channels == ("exact", "semantic")
    assert by_revision[exact_only.revision_id].channels == ("exact",)


async def test_hybrid_mode_degrades_to_exact_only_when_khoj_is_unavailable() -> None:
    exact_result = _result()
    exact = FakeExactSearch(SearchPage(items=(exact_result,), next_cursor=None))
    khoj = FakeKhojPort(raises=KhojUnavailableError("connection refused"))
    search = _search(exact=exact, khoj=khoj)

    page = await search(WORKSPACE, SearchQuery(q="aurora"), mode="hybrid")

    assert page.degraded is True
    assert [r.revision_id for r in page.items] == [exact_result.revision_id]


async def test_hybrid_mode_with_no_free_text_is_exact_only_and_not_degraded() -> None:
    exact_result = _result()
    exact = FakeExactSearch(SearchPage(items=(exact_result,), next_cursor=None))
    khoj = FakeKhojPort()
    search = _search(exact=exact, khoj=khoj)

    page = await search(WORKSPACE, SearchQuery(kind="project"), mode="hybrid")

    assert page.degraded is False
    assert [r.revision_id for r in page.items] == [exact_result.revision_id]
    assert khoj.search_queries == []


async def test_hybrid_mode_truncates_the_fused_union_to_the_requested_limit() -> None:
    """Each channel is independently capped to query.limit, so when the two
    channels barely overlap (a realistic case, not an edge case) the fused
    union can hold up to twice that many items before this truncation."""
    exact_results = tuple(_result() for _ in range(3))
    semantic_results = tuple(_result(channels=("semantic",)) for _ in range(3))
    exact = FakeExactSearch(SearchPage(items=exact_results, next_cursor=None))
    khoj_results = tuple(
        KhojSearchResult(entry="...", score=0.5, filename=f"ws/project/{i}.md") for i in range(3)
    )
    khoj = FakeKhojPort(search_results=khoj_results)
    hydrate = FakeSemanticHydrator({f"ws/project/{i}.md": semantic_results[i] for i in range(3)})
    search = _search(exact=exact, khoj=khoj, hydrate=hydrate)

    page = await search(WORKSPACE, SearchQuery(q="aurora", limit=3), mode="hybrid")

    assert len(page.items) == 3
