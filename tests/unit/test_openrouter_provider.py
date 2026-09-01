"""The OpenRouter adapter's model-routing safety.

docs/DESIGN.md 11: "Disable silent fallback between materially different
models for organization unless the fallback model is explicitly tested." A
gateway can route a request to a model other than the one pinned; recording
what was actually served is not enough on its own; a document derived from an
unapproved model is exactly the silent fallback the design forbids.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from tc_domain.llm import LLMError, LLMRequest, LLMStep, Message
from tc_infrastructure.llm.openrouter import OpenRouterProvider

MODEL_ID = "vendor/pinned-model"


def a_request() -> LLMRequest:
    return LLMRequest(
        step=LLMStep.ORGANIZE,
        messages=(Message(role="user", content="organize these"),),
        schema_name="Thing",
        json_schema={"type": "object"},
        prompt_version="v1",
        schema_version="v1",
    )


@dataclass
class FakeCompletion:
    payload: dict[str, Any]

    def model_dump(self) -> dict[str, Any]:
        return self.payload


def completion_payload(*, served_model: str, content: str = '{"ok": true}') -> dict[str, Any]:
    return {
        "id": "gen-123",
        "model": served_model,
        "provider": "synthetic-provider",
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


class FakeCompletions:
    def __init__(self, response: FakeCompletion) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def create(self, **params: Any) -> FakeCompletion:
        self.calls.append(params)
        return self._response


@dataclass
class FakeChat:
    completions: FakeCompletions


@dataclass
class FakeClient:
    chat: FakeChat = field(init=False)
    completions: FakeCompletions

    def __post_init__(self) -> None:
        self.chat = FakeChat(self.completions)


def a_provider(
    *,
    served_model: str,
    allowed_served_models: frozenset[str] | None = None,
    allow_fallbacks: bool = False,
    deny_data_collection: bool = True,
) -> tuple[OpenRouterProvider, FakeCompletions]:
    completions = FakeCompletions(FakeCompletion(completion_payload(served_model=served_model)))
    client = FakeClient(completions)
    provider = OpenRouterProvider(
        api_key="synthetic-key",
        base_url="https://synthetic.example/v1",
        model_id=MODEL_ID,
        supports_strict_schema=True,
        client=client,  # type: ignore[arg-type]
        allowed_served_models=allowed_served_models,
        allow_fallbacks=allow_fallbacks,
        deny_data_collection=deny_data_collection,
    )
    return provider, completions


async def test_a_response_from_the_requested_model_succeeds() -> None:
    provider, _ = a_provider(served_model=MODEL_ID)

    response = await provider.complete(a_request())

    assert response.model_served == MODEL_ID
    assert response.content == '{"ok": true}'


async def test_an_unapproved_served_model_is_rejected() -> None:
    """Silent routing to a different model must fail rather than be trusted."""
    provider, _ = a_provider(served_model="vendor/some-other-model")

    with pytest.raises(LLMError, match="unapproved"):
        await provider.complete(a_request())


async def test_an_explicitly_allowed_alternate_model_is_accepted() -> None:
    """A deployment that has tested and accepted a fallback may allow it."""
    provider, _ = a_provider(
        served_model="vendor/tested-fallback",
        allowed_served_models=frozenset({MODEL_ID, "vendor/tested-fallback"}),
    )

    response = await provider.complete(a_request())

    assert response.model_served == "vendor/tested-fallback"


# ---------------------------------------------------------------------------
# Request-side provider routing (docs/adr/0006)
# ---------------------------------------------------------------------------


def sent_provider_routing(completions: FakeCompletions) -> dict[str, Any]:
    """The `provider` object actually sent, unwrapped from `extra_body`.

    `provider` is an OpenRouter extension with no field on the OpenAI SDK's
    typed `create()`, so it travels via `extra_body` - this is what the
    adapter's caller (OpenRouter itself, here the fake) actually receives.
    """
    extra_body = completions.calls[0]["extra_body"]
    routing: dict[str, Any] = extra_body["provider"]
    return routing


async def test_the_conservative_defaults_deny_fallback_and_retention() -> None:
    """Constructing this class directly - a test, or an un-wired caller - must fail safe."""
    provider, completions = a_provider(served_model=MODEL_ID)

    await provider.complete(a_request())

    assert sent_provider_routing(completions) == {
        "allow_fallbacks": False,
        "data_collection": "deny",
    }


async def test_fallback_can_be_allowed_without_permitting_data_collection() -> None:
    """A custom-mode host can allow fallback without that also opening retention."""
    provider, completions = a_provider(
        served_model=MODEL_ID, allow_fallbacks=True, deny_data_collection=False
    )

    await provider.complete(a_request())

    routing = sent_provider_routing(completions)
    assert routing["allow_fallbacks"] is True
    assert "data_collection" not in routing


async def test_declining_to_deny_data_collection_omits_the_key_rather_than_allowing() -> None:
    """Not sending `data_collection` lets OpenRouter's own account default apply.

    Sending `data_collection: "allow"` explicitly would be this adapter
    choosing that on the operator's behalf; omitting the key leaves whatever
    the operator already configured on their own OpenRouter account.
    """
    provider, completions = a_provider(served_model=MODEL_ID, deny_data_collection=False)

    await provider.complete(a_request())

    assert "data_collection" not in sent_provider_routing(completions)
