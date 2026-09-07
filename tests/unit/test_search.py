"""``Search`` use-case mode-gating (docs/DESIGN.md 7.5, 10)."""

from __future__ import annotations

import uuid

import pytest

from tc_application.search import Search, UnsupportedSearchModeError
from tc_domain.capture import WorkspaceId
from tc_domain.search import SearchPage, SearchQuery, SearchResult
from tests.unit.fakes import FakeExactSearch

WORKSPACE = WorkspaceId(uuid.uuid4())


def _result() -> SearchResult:
    import datetime as dt

    return SearchResult(
        result_id="r1",
        document_id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        thought_ids=(),
        kind="project",
        title="A project",
        snippet="…",
        updated_at=dt.datetime.now(dt.UTC),
        entities=(),
        channels=("exact",),
        rank=1.0,
    )


async def test_exact_mode_delegates_to_the_exact_port() -> None:
    page = SearchPage(items=(_result(),), next_cursor=None)
    exact = FakeExactSearch(page)
    search = Search(exact)

    result = await search(WORKSPACE, SearchQuery(q="hello"), mode="exact")

    assert result is page
    assert exact.calls == [(WORKSPACE, SearchQuery(q="hello"))]


async def test_default_mode_is_exact() -> None:
    exact = FakeExactSearch()
    search = Search(exact)

    await search(WORKSPACE, SearchQuery())

    assert len(exact.calls) == 1


async def test_unknown_mode_raises() -> None:
    exact = FakeExactSearch()
    search = Search(exact)

    with pytest.raises(UnsupportedSearchModeError):
        await search(WORKSPACE, SearchQuery(), mode="not-a-real-mode")

    assert exact.calls == []


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
async def test_semantic_and_hybrid_degrade_when_no_khoj_is_wired(mode: str) -> None:
    """docs/adr/0010: a deployment with no Khoj adapter degrades exactly like
    an unreachable one, rather than raising or silently returning exact-only
    results without saying so."""
    exact = FakeExactSearch(SearchPage(items=(_result(),), next_cursor=None))
    search = Search(exact, khoj=None)

    page = await search(WORKSPACE, SearchQuery(q="hello"), mode=mode)

    assert page.degraded is True
    if mode == "hybrid":
        # Exact results are still returned, just flagged degraded - the
        # working channel's answer is not discarded.
        assert len(page.items) == 1
    else:
        assert page.items == ()
