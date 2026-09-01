"""The safe-mode allowlist (docs/adr/0006).

Every entry here is a claim that someone actually checked the model's
retention policy and strict-schema support - these tests only guard the shape
of that claim, not its truth.
"""

from __future__ import annotations

from tc_infrastructure.llm.reviewed_models import REVIEWED_MODELS, ReviewedModel


def test_every_entry_is_keyed_by_its_own_model_id() -> None:
    for slug, entry in REVIEWED_MODELS.items():
        assert entry.model_id == slug


def test_every_entry_records_at_least_one_reviewed_provider() -> None:
    """A model can be reviewed while a provider newly serving it has not been.

    ``providers`` is what restricts OpenRouter's *initial* routing choice
    (``provider.only``), not just backup attempts after a failure - an entry
    with no providers recorded would leave that restriction unenforceable.
    """
    for entry in REVIEWED_MODELS.values():
        assert entry.providers, f"{entry.model_id} has no reviewed provider recorded"


def test_the_allowlist_is_currently_empty() -> None:
    """qwen/qwen3.8-flash, the one staged candidate, was removed 2026-09-01.

    Its only OpenRouter endpoint (Alibaba) is not on OpenRouter's
    zero-data-retention endpoint list, and Alibaba's raw API-traffic
    retention policy was never independently confirmed - so it did not
    actually meet the "private" bar this registry exists to enforce. See
    ``reviewed_models.py``'s module docstring and docs/adr/0006. Safe mode
    with an empty allowlist runs organize/query-plan on the offline adapter
    rather than admit an unconfirmed-retention model on trust.
    """
    assert REVIEWED_MODELS == {}


def test_a_reviewed_model_records_all_required_fields() -> None:
    """Guards the dataclass shape independent of whatever is currently listed."""
    entry = ReviewedModel(
        model_id="test/synthetic-model",
        supports_strict_schema=True,
        providers=frozenset({"test-provider"}),
        note="synthetic entry for shape testing only",
    )
    assert entry.model_id == "test/synthetic-model"
    assert entry.providers == frozenset({"test-provider"})


def test_the_rejected_free_tier_candidate_is_not_reviewed() -> None:
    """It requires opting into training/publishing prompts - fails the privacy bar."""
    assert "nvidia/nemotron-3.5-lightning:free" not in REVIEWED_MODELS
