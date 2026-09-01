"""Entity naming and alias-merge policy (docs/DESIGN.md 6.4)."""

from __future__ import annotations

import pytest

from tc_domain.entities import (
    EntityResolutionThresholds,
    EntityType,
    InvalidThresholds,
    MatchDecision,
    document_stable_key,
    normalize_entity_name,
)


class TestNormalizeEntityName:
    def test_lowercases_and_strips(self) -> None:
        assert normalize_entity_name("  Jane Doe  ") == "jane doe"

    def test_collapses_internal_whitespace(self) -> None:
        assert normalize_entity_name("Jane   Doe") == "jane doe"

    def test_folds_accents(self) -> None:
        assert normalize_entity_name("José") == normalize_entity_name("Jose")

    def test_different_names_stay_different(self) -> None:
        assert normalize_entity_name("Jane") != normalize_entity_name("Joan")


class TestDocumentStableKey:
    def test_matches_the_design_example(self) -> None:
        assert (
            document_stable_key(EntityType.PROJECT, normalize_entity_name("Thought Capture AI"))
            == "project:thought-capture-ai"
        )

    def test_strips_leading_and_trailing_punctuation(self) -> None:
        assert document_stable_key(EntityType.PERSON, "jane!!") == "person:jane"


class TestEntityResolutionThresholds:
    def test_rejects_an_inverted_ordering(self) -> None:
        with pytest.raises(InvalidThresholds):
            EntityResolutionThresholds(merge_at=0.5, ambiguous_floor=0.8)

    def test_rejects_an_out_of_range_bound(self) -> None:
        with pytest.raises(InvalidThresholds):
            EntityResolutionThresholds(merge_at=1.2, ambiguous_floor=0.1)

    @pytest.mark.parametrize(
        ("similarity", "expected"),
        [
            (None, MatchDecision.NEW),
            (0.0, MatchDecision.NEW),
            (0.54, MatchDecision.NEW),
            (0.55, MatchDecision.AMBIGUOUS),
            (0.7, MatchDecision.AMBIGUOUS),
            (0.81, MatchDecision.AMBIGUOUS),
            (0.82, MatchDecision.MERGE),
            (1.0, MatchDecision.MERGE),
        ],
    )
    def test_classifies_by_the_configured_bounds(
        self, similarity: float | None, expected: MatchDecision
    ) -> None:
        thresholds = EntityResolutionThresholds(merge_at=0.82, ambiguous_floor=0.55)
        assert thresholds.classify(similarity) is expected
