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
) -> None:
    """Inject a synthetic allowlist entry.

    The real registry (``REVIEWED_MODELS``) is empty as of 2026-09-01 - its
    one candidate failed the private bar on review, see
    ``reviewed_models.py``. Tests of the safe-mode *mechanism* (accepts a
    reviewed slug, restricts to its recorded providers) must not depend on
    any specific model actually being reviewed, so they inject their own
    entry directly into the shared dict rather than naming a real slug.
    """
    monkeypatch.setitem(
        REVIEWED_MODELS,
        model_id,
        ReviewedModel(
            model_id=model_id,
            supports_strict_schema=True,
            providers=providers,
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
