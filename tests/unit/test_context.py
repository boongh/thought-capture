"""Deterministic context-assembly signals and budgeting (docs/DESIGN.md 7.3)."""

from __future__ import annotations

import datetime as dt

import pytest

from tc_domain.context import (
    ContextAssemblyConfig,
    Tier1Row,
    alias_signal,
    assemble,
    open_thread_signal,
    recency_signal,
)

NOW = dt.datetime(2026, 8, 31, 20, 0, tzinfo=dt.UTC)


def row(
    stable_key: str,
    *,
    canonical_name: str = "",
    aliases: tuple[str, ...] = (),
    last_mentioned_at: dt.datetime | None = None,
    open_thread_count: int = 0,
    summary: str = "",
) -> Tier1Row:
    return Tier1Row(
        stable_key=stable_key,
        entity_type="topic",
        canonical_name=canonical_name or stable_key,
        aliases=aliases,
        summary=summary,
        last_mentioned_at=last_mentioned_at,
        open_thread_count=open_thread_count,
    )


class TestAliasSignal:
    def test_matches_the_canonical_name(self) -> None:
        index = (row("project:aurora", canonical_name="Aurora"),)
        assert alias_signal("shipping the Aurora launch today", index) == {"project:aurora"}

    def test_matches_an_alias(self) -> None:
        index = (row("person:jane-doe", canonical_name="Jane Doe", aliases=("Jane",)),)
        assert alias_signal("talked to Jane about it", index) == {"person:jane-doe"}

    def test_does_not_match_an_unrelated_name(self) -> None:
        index = (row("person:jane-doe", canonical_name="Jane Doe"),)
        assert alias_signal("grocery list for tomorrow", index) == set()

    def test_is_accent_and_case_insensitive(self) -> None:
        index = (row("person:jose", canonical_name="José"),)
        assert alias_signal("caught up with JOSE yesterday", index) == {"person:jose"}


class TestRecencySignal:
    def test_includes_a_recent_mention(self) -> None:
        index = (row("topic:x", last_mentioned_at=NOW - dt.timedelta(days=1)),)
        assert recency_signal(index, now=NOW, recency_days=3) == {"topic:x"}

    def test_excludes_a_stale_mention(self) -> None:
        index = (row("topic:x", last_mentioned_at=NOW - dt.timedelta(days=10)),)
        assert recency_signal(index, now=NOW, recency_days=3) == set()

    def test_excludes_a_never_mentioned_document(self) -> None:
        index = (row("topic:x", last_mentioned_at=None),)
        assert recency_signal(index, now=NOW, recency_days=3) == set()

    def test_rejects_a_naive_now(self) -> None:
        index = (row("topic:x"),)
        with pytest.raises(ValueError, match="timezone-aware"):
            recency_signal(
                index,
                now=dt.datetime(2026, 8, 31),  # noqa: DTZ001
                recency_days=3,
            )


class TestOpenThreadSignal:
    def test_includes_a_document_with_open_threads(self) -> None:
        index = (row("todo:x", open_thread_count=2),)
        assert open_thread_signal(index) == {"todo:x"}

    def test_excludes_a_document_with_no_open_threads(self) -> None:
        index = (row("todo:x", open_thread_count=0),)
        assert open_thread_signal(index) == set()


class TestAssemble:
    def test_every_index_row_gets_a_selection(self) -> None:
        index = (row("a"), row("b"), row("c"))
        selections, _ = assemble(
            index,
            deterministic={
                "alias": frozenset({"a"}),
                "recency": frozenset(),
                "open_thread": frozenset(),
            },
        )
        assert {s.stable_key for s in selections} == {"a", "b", "c"}

    def test_a_deterministic_hit_is_full_inclusion(self) -> None:
        index = (row("a"), row("b"))
        selections, _ = assemble(
            index,
            deterministic={
                "alias": frozenset({"a"}),
                "recency": frozenset(),
                "open_thread": frozenset(),
            },
        )
        by_key = {s.stable_key: s for s in selections}
        assert by_key["a"].inclusion == "full"
        assert by_key["a"].signals == ("alias",)
        assert by_key["b"].inclusion == "index_only"
        assert by_key["b"].signals == ()

    def test_selector_can_add_a_document_the_deterministic_signals_missed(self) -> None:
        index = (row("a"),)
        selections, degraded = assemble(
            index,
            deterministic={
                "alias": frozenset(),
                "recency": frozenset(),
                "open_thread": frozenset(),
            },
            selector_keys=frozenset({"a"}),
        )
        assert selections[0].inclusion == "full"
        assert selections[0].signals == ("selector",)
        assert degraded is False

    def test_a_document_can_carry_multiple_signals(self) -> None:
        index = (row("a"),)
        selections, _ = assemble(
            index,
            deterministic={
                "alias": frozenset({"a"}),
                "recency": frozenset({"a"}),
                "open_thread": frozenset(),
            },
        )
        assert selections[0].signals == ("alias", "recency")

    def test_deterministic_hits_are_never_bumped_by_the_budget_before_selector_only_picks(
        self,
    ) -> None:
        """Invariant 2: the selector can add, never override, a deterministic hit."""
        index = (*(row(f"d{i}") for i in range(3)), row("s0"))
        selections, _ = assemble(
            index,
            deterministic={
                "alias": frozenset({"d0", "d1", "d2"}),
                "recency": frozenset(),
                "open_thread": frozenset(),
            },
            selector_keys=frozenset({"s0"}),
            config=ContextAssemblyConfig(max_selected_documents=1),
        )
        by_key = {s.stable_key: s.inclusion for s in selections}
        assert by_key["s0"] == "index_only", "the budget must exhaust on deterministic hits first"
        # Exactly one deterministic hit still gets `full` under the cap of 1.
        assert sum(1 for v in by_key.values() if v == "full") == 1

    def test_selector_failure_marks_the_run_degraded(self) -> None:
        index = (row("a"),)
        _, degraded = assemble(
            index,
            deterministic={
                "alias": frozenset(),
                "recency": frozenset(),
                "open_thread": frozenset(),
            },
            selector_degraded=True,
        )
        assert degraded is True

    def test_ordering_is_deterministic_across_two_calls(self) -> None:
        index = tuple(row(f"z{i}") for i in range(5))
        keys = frozenset(f"z{i}" for i in range(5))
        first, _ = assemble(
            index,
            deterministic={"alias": keys, "recency": frozenset(), "open_thread": frozenset()},
            config=ContextAssemblyConfig(max_selected_documents=2),
        )
        second, _ = assemble(
            index,
            deterministic={"alias": keys, "recency": frozenset(), "open_thread": frozenset()},
            config=ContextAssemblyConfig(max_selected_documents=2),
        )
        assert first == second
