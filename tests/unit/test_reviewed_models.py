"""The safe-mode allowlist (docs/adr/0006).

Every entry here is a claim that someone actually checked the model's
retention policy and strict-schema support - these tests only guard the shape
of that claim, not its truth.
"""

from __future__ import annotations

from tc_infrastructure.llm.reviewed_models import REVIEWED_MODELS


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


def test_the_researched_candidate_is_present_and_strict_schema_capable() -> None:
    """The candidate env.example staged before this ADR was written."""
    entry = REVIEWED_MODELS["qwen/qwen3.8-flash"]
    assert entry.supports_strict_schema is True
    assert entry.providers == frozenset({"alibaba"})


def test_the_rejected_free_tier_candidate_is_not_reviewed() -> None:
    """It requires opting into training/publishing prompts - fails the privacy bar."""
    assert "nvidia/nemotron-3.5-lightning:free" not in REVIEWED_MODELS
