"""Configuration parsing.

`env.example` is meant to be copied verbatim and filled in gradually. Anything
that makes the copied template fail to load is a defect in the template or in
the parser, not in the operator.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tc_infrastructure.config import Settings
from tc_infrastructure.llm.reviewed_models import REVIEWED_MODELS, ReviewedModel

REQUIRED_ENV = {
    "TC_WORKSPACE_TIMEZONE": "Asia/Bangkok",
}


def build(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in {**REQUIRED_ENV, **env}.items():
        monkeypatch.setenv(key, value)
    # _env_file=None keeps a developer's real .env out of the test.
    return Settings(_env_file=None)


def stub_reviewed_model(
    monkeypatch: pytest.MonkeyPatch,
    model_id: str,
    providers: frozenset[str] = frozenset({"test-provider"}),
    stages: frozenset[str] = frozenset({"organize", "select", "query_plan"}),
) -> None:
    """Inject a synthetic allowlist entry.

    Tests of the safe-mode *mechanism* (accepts a reviewed slug, restricts to
    its recorded providers/stage) must not depend on any specific model
    actually being reviewed, so they inject their own entry directly into the
    shared dict rather than naming a real slug. Defaults to every stage so
    that tests exercising the generic allowlist mechanism (not the
    stage-restriction itself) aren't tripped up by it - see
    ``stub_reviewed_model``'s ``stages`` override for tests that are.
    """
    monkeypatch.setitem(
        REVIEWED_MODELS,
        model_id,
        ReviewedModel(
            model_id=model_id,
            supports_strict_schema=True,
            providers=providers,
            stages=frozenset(stages),
            note="synthetic entry for test use only",
        ),
    )


def test_blank_optional_discord_ids_are_read_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Copying env.example leaves these blank; that must mean "not configured".

    A blank line in a .env file arrives as the empty string rather than as a
    missing key, so without coercion this fails validation at startup.
    """
    settings = build(
        monkeypatch,
        TC_DISCORD_OWNER_USER_ID="",
        TC_DISCORD_GUILD_ID="",
        TC_DISCORD_CHANNEL_ID="",
    )

    assert settings.discord_owner_user_id is None
    assert settings.discord_guild_id is None
    assert settings.discord_channel_id is None


def test_whitespace_only_discord_ids_are_read_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build(monkeypatch, TC_DISCORD_GUILD_ID="   ")
    assert settings.discord_guild_id is None


def test_populated_discord_ids_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build(
        monkeypatch,
        TC_DISCORD_OWNER_USER_ID="1234567890",
        TC_DISCORD_GUILD_ID="111",
        TC_DISCORD_CHANNEL_ID="222",
    )

    assert settings.discord_owner_user_id == 1234567890
    assert settings.discord_guild_id == 111
    assert settings.discord_channel_id == 222


def test_non_numeric_discord_id_is_still_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blank means unset; nonsense still fails loudly."""
    with pytest.raises(ValidationError):
        build(monkeypatch, TC_DISCORD_GUILD_ID="not-a-snowflake")


def test_unknown_timezone_is_rejected_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail on load rather than at 20:00 when the digest is due."""
    with pytest.raises(ValidationError, match="IANA"):
        build(monkeypatch, TC_WORKSPACE_TIMEZONE="Mars/Olympus_Mons")


def test_secrets_are_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A traceback or debug print must not leak the bot token."""
    settings = build(monkeypatch, TC_DISCORD_BOT_TOKEN="super-secret-token-value")

    assert "super-secret-token-value" not in repr(settings)
    assert settings.discord_bot_token.get_secret_value() == "super-secret-token-value"


def test_offline_model_adapter_is_selected_when_no_slug_is_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unpinned model must not silently fall through to a provider call."""
    assert build(monkeypatch, TC_MODEL_ORGANIZE="").uses_offline_model_adapter is True
    # custom mode: this test is about pinned-vs-unpinned, not about whether
    # the slug has been reviewed - the real allowlist may have no entries.
    pinned = build(
        monkeypatch, TC_MODEL_SELECTION_MODE="custom", TC_MODEL_ORGANIZE="some/pinned-model"
    )
    assert pinned.uses_offline_model_adapter is False


def test_safe_mode_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert build(monkeypatch).model_selection_mode == "safe"


def test_safe_mode_accepts_a_reviewed_model(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_reviewed_model(monkeypatch, "test/reviewed-model")
    settings = build(
        monkeypatch,
        TC_MODEL_SELECTION_MODE="safe",
        TC_MODEL_ORGANIZE="test/reviewed-model",
    )
    assert settings.model_organize == "test/reviewed-model"


def test_safe_mode_rejects_an_unreviewed_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """docs/adr/0006: a pinned slug must be reviewed before it can receive prompts."""
    with pytest.raises(ValidationError, match="docs/adr/0006"):
        build(
            monkeypatch,
            TC_MODEL_SELECTION_MODE="safe",
            TC_MODEL_ORGANIZE="nvidia/nemotron-3.5-lightning:free",
        )


def test_safe_mode_rejects_an_unreviewed_query_plan_model(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="docs/adr/0006"):
        build(monkeypatch, TC_MODEL_QUERY_PLAN="an/unreviewed-model")


def test_safe_mode_rejects_an_unreviewed_select_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """model_select makes a live provider call exactly like organize/query-plan
    (docs/DESIGN.md 7.3.2), so it gets the same safe-mode startup guarantee."""
    with pytest.raises(ValidationError, match="docs/adr/0006"):
        build(monkeypatch, TC_MODEL_SELECT="an/unreviewed-model")


def test_safe_mode_accepts_a_reviewed_select_model(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_reviewed_model(monkeypatch, "test/reviewed-model")
    settings = build(
        monkeypatch, TC_MODEL_SELECTION_MODE="safe", TC_MODEL_SELECT="test/reviewed-model"
    )
    assert settings.model_select == "test/reviewed-model"


def test_offline_select_adapter_is_selected_when_no_slug_is_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert build(monkeypatch, TC_MODEL_SELECT="").uses_offline_select_adapter is True
    stub_reviewed_model(monkeypatch, "test/reviewed-model")
    pinned = build(monkeypatch, TC_MODEL_SELECT="test/reviewed-model")
    assert pinned.uses_offline_select_adapter is False


# ---------------------------------------------------------------------------
# Stage restriction: a model reviewed for one stage must not be usable in
# another, even though it is a REVIEWED_MODELS key (Codex PR review finding -
# safe mode previously accepted TC_MODEL_SELECT=x-ai/grok-4.3 and
# TC_MODEL_ORGANIZE=upstage/solar-pro4 despite both being registered
# select-only/organize-only respectively).
# ---------------------------------------------------------------------------


def test_safe_mode_rejects_the_real_select_only_model_pinned_to_organize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """upstage/solar-pro4 is reviewed for select only (reviewed_models.py)."""
    with pytest.raises(ValidationError, match="docs/adr/0006"):
        build(monkeypatch, TC_MODEL_SELECTION_MODE="safe", TC_MODEL_ORGANIZE="upstage/solar-pro4")


def test_safe_mode_rejects_the_real_organize_only_model_pinned_to_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """x-ai/grok-4.3 is reviewed for organize only (reviewed_models.py)."""
    with pytest.raises(ValidationError, match="docs/adr/0006"):
        build(monkeypatch, TC_MODEL_SELECTION_MODE="safe", TC_MODEL_SELECT="x-ai/grok-4.3")


def test_safe_mode_rejects_the_real_organize_only_model_pinned_to_query_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="docs/adr/0006"):
        build(monkeypatch, TC_MODEL_SELECTION_MODE="safe", TC_MODEL_QUERY_PLAN="x-ai/grok-4.3")


def test_safe_mode_rejects_a_stage_crossed_synthetic_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mechanism-level proof, independent of which real slugs are currently
    registered: a model reviewed only for ``select`` must be rejected for
    ``organize``."""
    stub_reviewed_model(monkeypatch, "test/select-only-model", stages=frozenset({"select"}))
    with pytest.raises(ValidationError, match="docs/adr/0006"):
        build(
            monkeypatch,
            TC_MODEL_SELECTION_MODE="safe",
            TC_MODEL_ORGANIZE="test/select-only-model",
        )


def test_safe_mode_accepts_a_stage_crossed_synthetic_model_for_its_own_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_reviewed_model(monkeypatch, "test/select-only-model", stages=frozenset({"select"}))
    settings = build(
        monkeypatch,
        TC_MODEL_SELECTION_MODE="safe",
        TC_MODEL_SELECT="test/select-only-model",
    )
    assert settings.model_select == "test/select-only-model"


def test_custom_mode_ignores_stage_restriction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape hatch still applies: custom mode accepts any slug for any
    field, stage-crossed or not."""
    settings = build(
        monkeypatch, TC_MODEL_SELECTION_MODE="custom", TC_MODEL_ORGANIZE="upstage/solar-pro4"
    )
    assert settings.model_organize == "upstage/solar-pro4"


def test_safe_mode_permits_an_empty_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty selects the offline adapter, not a provider call - nothing to review."""
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="safe", TC_MODEL_ORGANIZE="")
    assert settings.uses_offline_model_adapter is True


def test_custom_mode_accepts_an_unreviewed_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape hatch: the host accepts responsibility for the model themselves."""
    settings = build(
        monkeypatch,
        TC_MODEL_SELECTION_MODE="custom",
        TC_MODEL_ORGANIZE="nvidia/nemotron-3.5-lightning:free",
    )
    assert settings.model_organize == "nvidia/nemotron-3.5-lightning:free"


def test_an_unknown_selection_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        build(monkeypatch, TC_MODEL_SELECTION_MODE="reckless")


def test_safe_mode_never_allows_fallback_even_if_the_flag_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/adr/0006: a reviewed model was vetted, whatever it might fall back to was not."""
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="safe", TC_MODEL_ALLOW_FALLBACK="true")
    assert settings.openrouter_allow_fallbacks is False


def test_safe_mode_always_denies_data_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="safe")
    assert settings.openrouter_deny_data_collection is True


def test_custom_mode_follows_the_fallback_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    allow = build(monkeypatch, TC_MODEL_SELECTION_MODE="custom", TC_MODEL_ALLOW_FALLBACK="true")
    deny = build(monkeypatch, TC_MODEL_SELECTION_MODE="custom", TC_MODEL_ALLOW_FALLBACK="false")
    assert allow.openrouter_allow_fallbacks is True
    assert deny.openrouter_allow_fallbacks is False


def test_custom_mode_never_forces_data_collection_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forcing deny here would silently break the documented free-tier custom-mode use case."""
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="custom")
    assert settings.openrouter_deny_data_collection is False


def test_fallback_defaults_to_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matches OpenRouter's own default, so custom mode without this set behaves as documented."""
    assert build(monkeypatch).model_allow_fallback is True


def test_safe_mode_always_requires_zdr_even_if_the_flag_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/adr/0006: zdr is a stricter, independent check from data_collection - safe
    mode requires both, unconditionally."""
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="safe", TC_MODEL_REQUIRE_ZDR="false")
    assert settings.openrouter_require_zdr is True


def test_custom_mode_follows_the_zdr_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    require = build(monkeypatch, TC_MODEL_SELECTION_MODE="custom", TC_MODEL_REQUIRE_ZDR="true")
    decline = build(monkeypatch, TC_MODEL_SELECTION_MODE="custom", TC_MODEL_REQUIRE_ZDR="false")
    assert require.openrouter_require_zdr is True
    assert decline.openrouter_require_zdr is False


def test_zdr_requirement_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matches OpenRouter's own default (no additional request-level restriction)."""
    assert build(monkeypatch).model_require_zdr is False


def test_safe_mode_restricts_a_reviewed_model_to_its_reviewed_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_reviewed_model(monkeypatch, "test/reviewed-model", providers=frozenset({"test-provider"}))
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="safe")
    assert settings.openrouter_only_providers("test/reviewed-model") == frozenset({"test-provider"})


def test_safe_mode_has_no_restriction_for_a_model_not_looked_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not a realistic call in practice - the validator already rejects an unreviewed
    pin - but the lookup itself must not fabricate a restriction for a model it
    doesn't recognize."""
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="safe")
    assert settings.openrouter_only_providers("an/unreviewed-model") is None


def test_custom_mode_never_restricts_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even for a slug that happens to also be reviewed - custom mode's promise is
    freedom from the allowlist entirely, not a silent partial application of it."""
    stub_reviewed_model(monkeypatch, "test/reviewed-model")
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="custom")
    assert settings.openrouter_only_providers("test/reviewed-model") is None


# ---------------------------------------------------------------------------
# strict_schema_supported (PR #16 review: safe mode must derive strict-schema
# enforcement from REVIEWED_MODELS, not from the mutable
# model_supports_strict_schema / model_select_supports_strict_schema flags)
# ---------------------------------------------------------------------------


def test_safe_mode_derives_strict_schema_from_the_reviewed_model_even_if_the_flag_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact PR #16 finding: a reviewed model's own recorded capability
    must win over an operator-set flag left at its false default."""
    stub_reviewed_model(monkeypatch, "test/reviewed-model")
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="safe")
    assert settings.strict_schema_supported("test/reviewed-model", custom_flag=False) is True


def test_safe_mode_ignores_a_flag_that_disagrees_with_the_reviewed_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Safe mode is a hard-enforced allowlist: an operator cannot silently
    weaken it by setting the flag `True` either, when nothing derives from
    that flag in safe mode in the first place - the registry alone decides."""
    stub_reviewed_model(monkeypatch, "test/reviewed-model")
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="safe")
    assert settings.strict_schema_supported("test/reviewed-model", custom_flag=True) is True


def test_safe_mode_has_no_strict_schema_support_for_a_model_not_looked_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not a realistic call in practice - the validator already rejects an
    unreviewed pin - but the lookup itself must not fabricate support for a
    model it doesn't recognize, same defensive shape as openrouter_only_providers."""
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="safe")
    assert settings.strict_schema_supported("an/unreviewed-model", custom_flag=True) is False


def test_custom_mode_follows_the_strict_schema_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Custom mode is exactly where an operator-set override is legitimate -
    the operator has already opted out of the allowlist."""
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="custom")
    assert settings.strict_schema_supported("an/unreviewed-model", custom_flag=True) is True
    assert settings.strict_schema_supported("an/unreviewed-model", custom_flag=False) is False


def test_custom_mode_ignores_the_registry_for_strict_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even for a slug that happens to also be reviewed - custom mode's
    promise is freedom from the allowlist entirely."""
    stub_reviewed_model(monkeypatch, "test/reviewed-model")
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="custom")
    assert settings.strict_schema_supported("test/reviewed-model", custom_flag=False) is False


# ---------------------------------------------------------------------------
# reasoning_effort_for (docs/model-evaluation-organize-select.md round 8: a
# reasoning model's safe cost profile is a reviewed fact, same shape as
# strict_schema_supported)
# ---------------------------------------------------------------------------


def test_reasoning_effort_for_a_reviewed_model_with_no_recorded_effort_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-reasoning reviewed model (the synthetic stub, matching
    upstage/solar-pro4) has no reasoning_effort to control."""
    stub_reviewed_model(monkeypatch, "test/reviewed-model")
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="safe")
    assert settings.reasoning_effort_for("test/reviewed-model") is None


def test_reasoning_effort_for_grok_4_3_is_none_by_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real registry entry, not a synthetic stub: round 8 found
    reasoning_effort="none" close to a requirement for x-ai/grok-4.3 to fit
    the operational cost cap, so it is recorded on the entry itself."""
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="safe")
    assert settings.reasoning_effort_for("x-ai/grok-4.3") == "none"


def test_reasoning_effort_for_a_model_not_looked_up_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not a realistic call in practice - the validator already rejects an
    unreviewed pin - but the lookup itself must not fabricate a value for a
    model it doesn't recognize, same defensive shape as the other two
    registry-derived methods."""
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="safe")
    assert settings.reasoning_effort_for("an/unreviewed-model") is None


def test_custom_mode_ignores_the_registry_for_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom mode's promise is freedom from the allowlist entirely, even for
    a slug that happens to also be reviewed with a recorded effort level."""
    settings = build(monkeypatch, TC_MODEL_SELECTION_MODE="custom")
    assert settings.reasoning_effort_for("x-ai/grok-4.3") is None
