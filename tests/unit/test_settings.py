"""Configuration parsing.

`env.example` is meant to be copied verbatim and filled in gradually. Anything
that makes the copied template fail to load is a defect in the template or in
the parser, not in the operator.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tc_infrastructure.config import Settings

REQUIRED_ENV = {
    "TC_WORKSPACE_TIMEZONE": "Asia/Bangkok",
}


def build(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in {**REQUIRED_ENV, **env}.items():
        monkeypatch.setenv(key, value)
    # _env_file=None keeps a developer's real .env out of the test.
    return Settings(_env_file=None)


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
    pinned = build(monkeypatch, TC_MODEL_ORGANIZE="qwen/qwen3.8-flash")
    assert pinned.uses_offline_model_adapter is False


def test_safe_mode_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert build(monkeypatch).model_selection_mode == "safe"


def test_safe_mode_accepts_a_reviewed_model(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build(
        monkeypatch,
        TC_MODEL_SELECTION_MODE="safe",
        TC_MODEL_ORGANIZE="qwen/qwen3.8-flash",
    )
    assert settings.model_organize == "qwen/qwen3.8-flash"


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
