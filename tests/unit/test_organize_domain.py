"""Coverage validation (docs/DESIGN.md 7.2 step 8)."""

from __future__ import annotations

import pytest

from tc_domain.capture import ThoughtId
from tc_domain.organize import OrganizeCoverageError, validate_coverage


def ids(*values: int) -> frozenset[ThoughtId]:
    return frozenset(ThoughtId(v) for v in values)


def test_every_thought_cited_or_unorganized_passes() -> None:
    validate_coverage(ids(1, 2, 3), cited_thought_ids=ids(1, 2), unorganized_thought_ids=ids(3))


def test_a_missing_thought_is_rejected() -> None:
    with pytest.raises(OrganizeCoverageError, match="neither cited nor marked"):
        validate_coverage(ids(1, 2, 3), cited_thought_ids=ids(1, 2), unorganized_thought_ids=ids())


def test_a_reference_outside_the_window_is_rejected() -> None:
    with pytest.raises(OrganizeCoverageError, match="outside this window"):
        validate_coverage(
            ids(1, 2), cited_thought_ids=ids(1, 2, 999), unorganized_thought_ids=ids()
        )


def test_a_thought_cited_and_marked_unorganized_is_rejected() -> None:
    with pytest.raises(OrganizeCoverageError, match="both cited and marked unorganized"):
        validate_coverage(ids(1), cited_thought_ids=ids(1), unorganized_thought_ids=ids(1))


def test_an_empty_window_with_no_output_passes() -> None:
    validate_coverage(ids(), cited_thought_ids=ids(), unorganized_thought_ids=ids())
