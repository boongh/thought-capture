"""Per-step provider construction (`docs/DESIGN.md` 11: organize and select
are independently pinned models)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from tc_domain.llm import LLMRequest, LLMStep, Message
from tc_infrastructure.config import Settings
from tc_infrastructure.llm.factory import build_organize_provider, build_select_provider
from tc_infrastructure.llm.offline import OfflineLLMProvider
from tc_infrastructure.llm.openrouter import OpenRouterProvider
from tc_infrastructure.llm.reviewed_models import REVIEWED_MODELS, ReviewedModel

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


# ---------------------------------------------------------------------------
# Safe mode: strict-schema enforcement must come from REVIEWED_MODELS, not
# from the mutable model_*_supports_strict_schema flags (PR #16 review finding).
# ---------------------------------------------------------------------------


def test_safe_mode_select_provider_is_strict_even_though_the_flag_defaults_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact reported bug: TC_MODEL_SELECT=upstage/solar-pro4 in safe mode,
    with TC_MODEL_SELECT_SUPPORTS_STRICT_SCHEMA left unset (defaults False),
    must still build a strict-schema provider - REVIEWED_MODELS records that
    upstage/solar-pro4 was confirmed to support it
    (docs/model-evaluation-organize-select.md)."""
    settings = build(
        monkeypatch,
        TC_MODEL_SELECTION_MODE="safe",
        TC_MODEL_SELECT="upstage/solar-pro4",
    )
    assert settings.model_select_supports_strict_schema is False  # the flag really is at default

    provider = build_select_provider(settings)

    assert isinstance(provider, OpenRouterProvider)
    assert provider.model_id == "upstage/solar-pro4"
    assert provider.supports_strict_schema is True


def test_safe_mode_organize_provider_derives_strict_schema_from_the_registry_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same fix, same mechanism, for organize - proven with a synthetic
    registry entry since the real registry has no reviewed organize
    candidate yet (docs/adr/0006)."""
    monkeypatch.setitem(
        REVIEWED_MODELS,
        "test/reviewed-organize-model",
        ReviewedModel(
            model_id="test/reviewed-organize-model",
            supports_strict_schema=True,
            providers=frozenset({"test-provider"}),
            note="synthetic entry for test use only",
        ),
    )
    settings = build(
        monkeypatch,
        TC_MODEL_SELECTION_MODE="safe",
        TC_MODEL_ORGANIZE="test/reviewed-organize-model",
    )
    assert settings.model_supports_strict_schema is False  # the flag really is at default

    provider = build_organize_provider(settings)

    assert isinstance(provider, OpenRouterProvider)
    assert provider.supports_strict_schema is True


# ---------------------------------------------------------------------------
# Request-level regression test: the built provider's *outgoing request*, not
# just its internal supports_strict_schema flag, must carry strict
# response_format and provider.require_parameters: true.
# ---------------------------------------------------------------------------


@dataclass
class _FakeCompletion:
    payload: dict[str, Any]

    def model_dump(self) -> dict[str, Any]:
        return self.payload


class _FakeCompletions:
    def __init__(self, response: _FakeCompletion) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **params: Any) -> _FakeCompletion:
        self.calls.append(params)
        return self._response


@dataclass
class _FakeChat:
    completions: _FakeCompletions


@dataclass
class _FakeClient:
    chat: _FakeChat = field(init=False)
    completions: _FakeCompletions

    def __post_init__(self) -> None:
        self.chat = _FakeChat(self.completions)


def _a_select_request() -> LLMRequest:
    return LLMRequest(
        step=LLMStep.SELECT,
        messages=(Message(role="user", content="select the relevant documents"),),
        schema_name="SelectedContext",
        json_schema={"type": "object"},
        prompt_version="v1",
        schema_version="v1",
    )


async def test_safe_mode_select_request_actually_carries_strict_schema_and_require_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/DESIGN.md 11 / docs/adr/0006's safe-mode admission requirements are
    only real if the *outgoing OpenRouter request* reflects them, not just an
    internal flag. Safe mode + TC_MODEL_SELECT=upstage/solar-pro4 +
    TC_MODEL_SELECT_SUPPORTS_STRICT_SCHEMA left at its false default must
    still send strict response_format and provider.require_parameters: true,
    because REVIEWED_MODELS records upstage/solar-pro4 as confirmed to
    support strict schema enforcement - the operator-set flag must not be
    able to silently drop that in safe mode (PR #16 review)."""
    settings = build(
        monkeypatch,
        TC_MODEL_SELECTION_MODE="safe",
        TC_MODEL_SELECT="upstage/solar-pro4",
    )
    completions = _FakeCompletions(
        _FakeCompletion(
            {
                "id": "gen-123",
                "model": "upstage/solar-pro4",
                "provider": "upstage",
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )
    )
    provider = build_select_provider(settings, client=_FakeClient(completions))  # type: ignore[arg-type]

    await provider.complete(_a_select_request())

    assert len(completions.calls) == 1
    sent = completions.calls[0]
    assert sent["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "SelectedContext",
            "strict": True,
            "schema": {"type": "object"},
        },
    }
    assert sent["extra_body"]["provider"]["require_parameters"] is True
