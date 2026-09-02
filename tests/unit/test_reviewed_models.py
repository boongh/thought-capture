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


def test_organize_has_no_reviewed_candidate() -> None:
    """Round 4's organize recommendation did not survive round 5's higher-N retest.

    ``meta-llama/llama-4-maverick`` (docs/model-evaluation-organize-select.md)
    surfaced a reproducible structural defect under higher-N testing and was
    not added to the registry pending further evidence or an owner decision.
    Safe mode with no reviewed organize candidate runs organize on the
    offline adapter rather than admit an unresolved candidate on trust.
    """
    assert "meta-llama/llama-4-maverick" not in REVIEWED_MODELS


def test_select_has_exactly_one_reviewed_candidate() -> None:
    """upstage/solar-pro4 is the first model to pass all three review bars.

    See ``reviewed_models.py``'s module docstring and
    docs/model-evaluation-organize-select.md for the five-round evaluation
    this entry is based on.
    """
    assert set(REVIEWED_MODELS) == {"upstage/solar-pro4"}
    entry = REVIEWED_MODELS["upstage/solar-pro4"]
    assert entry.supports_strict_schema is True
    assert entry.providers == frozenset({"upstage/zdr"})


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
