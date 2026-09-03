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


def test_every_entry_records_at_least_one_reviewed_stage() -> None:
    """A model's review is stage-specific (docs/DESIGN.md 7.3.2, 7.4) - an
    entry with no recorded stage would be usable nowhere, which almost
    certainly means the stage was simply forgotten when the entry was added.
    """
    for entry in REVIEWED_MODELS.values():
        assert entry.stages, f"{entry.model_id} has no reviewed stage recorded"


def test_ruled_out_organize_candidates_are_not_reviewed() -> None:
    """Round 4/5/12's organize candidates that did not survive evaluation.

    ``meta-llama/llama-4-maverick`` (docs/model-evaluation-organize-select.md)
    surfaced a reproducible structural defect under higher-N testing;
    ``x-ai/grok-build-0.1`` (round 12) asserted the fabrication-A planted
    claim as confirmed fact, a worse safety showing than the reviewed
    ``x-ai/grok-4.3`` at higher cost. Neither was added to the registry.
    """
    assert "meta-llama/llama-4-maverick" not in REVIEWED_MODELS
    assert "x-ai/grok-build-0.1" not in REVIEWED_MODELS


def test_select_has_exactly_one_reviewed_candidate() -> None:
    """upstage/solar-pro4 is the first model to pass all three review bars.

    See ``reviewed_models.py``'s module docstring and
    docs/model-evaluation-organize-select.md for the five-round evaluation
    this entry is based on.
    """
    entry = REVIEWED_MODELS["upstage/solar-pro4"]
    assert entry.supports_strict_schema is True
    assert entry.providers == frozenset({"upstage/zdr"})
    assert entry.stages == frozenset({"select"})
    assert entry.reasoning_effort is None


def test_organize_has_exactly_one_reviewed_candidate() -> None:
    """x-ai/grok-4.3 is the first organize model to pass all three review bars.

    See ``reviewed_models.py``'s module docstring and
    docs/model-evaluation-organize-select.md rounds 8-13 for the evaluation
    this entry is based on. ``reasoning_effort="none"`` is recorded on the
    entry itself, not left to an operator flag - round 8 found it close to
    a requirement, not an optional optimization, for this candidate's cost
    to fit the project's operational cap.
    """
    entry = REVIEWED_MODELS["x-ai/grok-4.3"]
    assert entry.supports_strict_schema is True
    assert entry.providers == frozenset({"xai/zdr"})
    assert entry.stages == frozenset({"organize"})
    assert entry.reasoning_effort == "none"


def test_the_registry_has_exactly_these_two_entries() -> None:
    """Guards against a future addition landing without an accompanying test."""
    assert set(REVIEWED_MODELS) == {"upstage/solar-pro4", "x-ai/grok-4.3"}


def test_a_reviewed_model_records_all_required_fields() -> None:
    """Guards the dataclass shape independent of whatever is currently listed."""
    entry = ReviewedModel(
        model_id="test/synthetic-model",
        supports_strict_schema=True,
        providers=frozenset({"test-provider"}),
        stages=frozenset({"organize"}),
        note="synthetic entry for shape testing only",
    )
    assert entry.model_id == "test/synthetic-model"
    assert entry.providers == frozenset({"test-provider"})
    assert entry.stages == frozenset({"organize"})


def test_the_rejected_free_tier_candidate_is_not_reviewed() -> None:
    """It requires opting into training/publishing prompts - fails the privacy bar."""
    assert "nvidia/nemotron-3.5-lightning:free" not in REVIEWED_MODELS
