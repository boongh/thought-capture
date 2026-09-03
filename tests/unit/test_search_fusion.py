"""``fuse_rrf`` - Reciprocal Rank Fusion invariants (docs/DESIGN.md 7.5, 15.1)."""

from __future__ import annotations

import datetime as dt
import uuid

from hypothesis import given
from hypothesis import strategies as st

from tc_domain.search import SearchResult
from tc_domain.search_fusion import fuse_rrf


def _result(
    *, revision_id: uuid.UUID | None = None, channels: tuple[str, ...] = ("exact",)
) -> SearchResult:
    return SearchResult(
        result_id=str(uuid.uuid4()),
        document_id=uuid.uuid4(),
        revision_id=revision_id or uuid.uuid4(),
        thought_ids=(),
        kind="project",
        title="A project",
        snippet="…",
        updated_at=dt.datetime.now(dt.UTC),
        entities=(),
        channels=channels,  # type: ignore[arg-type]
        rank=0.0,
    )


def _results(n: int) -> tuple[SearchResult, ...]:
    return tuple(_result() for _ in range(n))


# ---------------------------------------------------------------------------
# Example-based
# ---------------------------------------------------------------------------


def test_a_result_in_only_one_channel_is_carried_through_unchanged_in_identity() -> None:
    exact_only = _result()
    fused = fuse_rrf((exact_only,), ())

    assert len(fused) == 1
    assert fused[0].revision_id == exact_only.revision_id
    assert fused[0].channels == ("exact",)


def test_a_result_in_both_channels_is_deduplicated_and_carries_both_channels() -> None:
    shared_id = uuid.uuid4()
    exact_side = _result(revision_id=shared_id, channels=("exact",))
    semantic_side = _result(revision_id=shared_id, channels=("semantic",))

    fused = fuse_rrf((exact_side,), (semantic_side,))

    assert len(fused) == 1
    assert fused[0].channels == ("exact", "semantic")


def test_a_result_in_both_channels_scores_higher_than_either_channel_alone() -> None:
    shared_id = uuid.uuid4()
    exact_side = _result(revision_id=shared_id, channels=("exact",))
    semantic_side = _result(revision_id=shared_id, channels=("semantic",))
    only_in_exact = _result()

    fused = fuse_rrf((exact_side, only_in_exact), (semantic_side,))

    by_id = {r.revision_id: r for r in fused}
    assert by_id[shared_id].rank > by_id[only_in_exact.revision_id].rank


def test_fusing_against_an_empty_channel_preserves_the_other_channels_order() -> None:
    first, second, third = _results(3)

    fused = fuse_rrf((first, second, third), ())

    assert [r.revision_id for r in fused] == [
        first.revision_id,
        second.revision_id,
        third.revision_id,
    ]


def test_both_channels_empty_returns_empty() -> None:
    assert fuse_rrf((), ()) == ()


def test_phrase_boost_never_lets_a_lower_ranked_exact_result_pass_a_higher_ranked_one() -> None:
    """docs/DESIGN.md 7.5: the boost applies to every exact-channel result
    when a phrase was matched (PostgresExactSearch ANDs phrase with every
    other filter), so it must not reorder exact-only results relative to
    each other - only relative to semantic-only results."""
    first, second, third = _results(3)

    fused = fuse_rrf((first, second, third), (), has_phrase_match=True)

    assert [r.revision_id for r in fused] == [
        first.revision_id,
        second.revision_id,
        third.revision_id,
    ]


def test_phrase_boost_raises_an_exact_result_above_an_otherwise_higher_ranked_semantic_one() -> (
    None
):
    # exact_result sits at rank 3 in its own channel (behind two decoys), so
    # its unboosted RRF contribution (1/63) is genuinely smaller than
    # semantic_result's rank-1 contribution (1/61) - a real rank difference,
    # not the tie a single-item-per-channel setup would produce.
    decoy_a, decoy_b = _results(2)
    exact_result = _result()
    semantic_result = _result()

    without_boost = fuse_rrf((decoy_a, decoy_b, exact_result), (semantic_result,))
    without_boost_order = [r.revision_id for r in without_boost]
    assert without_boost_order.index(semantic_result.revision_id) < without_boost_order.index(
        exact_result.revision_id
    )

    with_boost = fuse_rrf(
        (decoy_a, decoy_b, exact_result), (semantic_result,), has_phrase_match=True
    )
    with_boost_order = [r.revision_id for r in with_boost]
    assert with_boost_order.index(exact_result.revision_id) < with_boost_order.index(
        semantic_result.revision_id
    )


def test_result_order_is_deterministic_for_the_same_inputs() -> None:
    first, second, third = _results(3)
    exact = (first, second, third)
    semantic = (second, third)

    a = fuse_rrf(exact, semantic)
    b = fuse_rrf(exact, semantic)

    assert [r.revision_id for r in a] == [r.revision_id for r in b]


# ---------------------------------------------------------------------------
# Property-based
# ---------------------------------------------------------------------------


@given(
    exact_n=st.integers(min_value=0, max_value=8), semantic_n=st.integers(min_value=0, max_value=8)
)
def test_fused_output_never_has_duplicate_revision_ids(exact_n: int, semantic_n: int) -> None:
    exact = _results(exact_n)
    # Half the semantic results overlap with the exact ones, half are distinct -
    # exercises both the dedup and the passthrough paths in one property.
    overlap = exact[: semantic_n // 2]
    semantic = overlap + _results(max(0, semantic_n - len(overlap)))

    fused = fuse_rrf(exact, semantic)

    revision_ids = [r.revision_id for r in fused]
    assert len(revision_ids) == len(set(revision_ids))


@given(
    exact_n=st.integers(min_value=0, max_value=8), semantic_n=st.integers(min_value=0, max_value=8)
)
def test_fused_output_covers_the_union_of_both_channels(exact_n: int, semantic_n: int) -> None:
    exact = _results(exact_n)
    semantic = _results(semantic_n)

    fused = fuse_rrf(exact, semantic)

    expected = {r.revision_id for r in exact} | {r.revision_id for r in semantic}
    assert {r.revision_id for r in fused} == expected


@given(
    exact_n=st.integers(min_value=0, max_value=8), semantic_n=st.integers(min_value=0, max_value=8)
)
def test_fused_scores_are_sorted_descending(exact_n: int, semantic_n: int) -> None:
    exact = _results(exact_n)
    semantic = _results(semantic_n)

    fused = fuse_rrf(exact, semantic)

    ranks = [r.rank for r in fused]
    assert ranks == sorted(ranks, reverse=True)
