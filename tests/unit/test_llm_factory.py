"""Per-step provider construction (`docs/DESIGN.md` 11: organize and select
are independently pinned models)."""

from __future__ import annotations

import pytest

from tc_infrastructure.config import Settings
from tc_infrastructure.llm.factory import build_organize_provider, build_select_provider
from tc_infrastructure.llm.offline import OfflineLLMProvider
from tc_infrastructure.llm.openrouter import OpenRouterProvider

REQUIRED_ENV = {
    "TC_WORKSPACE_TIMEZONE": "Asia/Bangkok",
    # AsyncOpenAI (constructed by OpenRouterProvider) requires a non-empty
    # api_key at construction time, even though these tests never call
    # `.complete()` and so never make a network request.
    "TC_OPENROUTER_API_KEY": "test-key",
}


def build(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in {**REQUIRED_ENV, **env}.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_build_organize_provider_is_offline_when_unpinned(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build(monkeypatch, TC_MODEL_ORGANIZE="")
    assert isinstance(build_organize_provider(settings), OfflineLLMProvider)


def test_build_select_provider_is_offline_when_unpinned(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build(monkeypatch, TC_MODEL_SELECT="")
    assert isinstance(build_select_provider(settings), OfflineLLMProvider)


def test_build_organize_provider_pins_the_organize_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Custom mode: proves the factory wires the right slug/flag, independent
    of whether that slug has actually been reviewed (test_settings.py covers
    the safe-mode allowlist gate itself)."""
    settings = build(
        monkeypatch,
        TC_MODEL_SELECTION_MODE="custom",
        TC_MODEL_ORGANIZE="some/organize-model",
        TC_MODEL_SUPPORTS_STRICT_SCHEMA="true",
    )
    provider = build_organize_provider(settings)
    assert isinstance(provider, OpenRouterProvider)
    assert provider.model_id == "some/organize-model"
    assert provider.supports_strict_schema is True


def test_build_select_provider_pins_the_select_model(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build(
        monkeypatch,
        TC_MODEL_SELECTION_MODE="custom",
        TC_MODEL_SELECT="some/select-model",
        TC_MODEL_SELECT_SUPPORTS_STRICT_SCHEMA="true",
    )
    provider = build_select_provider(settings)
    assert isinstance(provider, OpenRouterProvider)
    assert provider.model_id == "some/select-model"
    assert provider.supports_strict_schema is True


def test_organize_and_select_providers_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of this factory split: one Settings instance can pin
    two different models for the two steps, and each provider only carries
    its own step's slug and strict-schema flag."""
    settings = build(
        monkeypatch,
        TC_MODEL_SELECTION_MODE="custom",
        TC_MODEL_ORGANIZE="some/organize-model",
        TC_MODEL_SUPPORTS_STRICT_SCHEMA="false",
        TC_MODEL_SELECT="some/select-model",
        TC_MODEL_SELECT_SUPPORTS_STRICT_SCHEMA="true",
    )
    organize_provider = build_organize_provider(settings)
    select_provider = build_select_provider(settings)

    assert isinstance(organize_provider, OpenRouterProvider)
    assert isinstance(select_provider, OpenRouterProvider)
    assert organize_provider.model_id == "some/organize-model"
    assert select_provider.model_id == "some/select-model"
    assert organize_provider.supports_strict_schema is False
    assert select_provider.supports_strict_schema is True
