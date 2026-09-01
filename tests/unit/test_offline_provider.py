"""The deterministic offline provider.

It is selected whenever no model slug is pinned, so an unconfigured deployment
runs the pipeline without silently calling a provider. Its defining property is
that it never invents content: a run against it proves orchestration,
validation, persistence, and provenance - never generation quality.
"""

from __future__ import annotations

import pytest

from tc_domain.llm import LLMError, LLMRequest, LLMStep, Message
from tc_infrastructure.llm.offline import (
    OFFLINE_MODEL_ID,
    OfflineLLMProvider,
    request_fingerprint,
)


def a_request(
    *, content: str = "organize these", step: LLMStep = LLMStep.ORGANIZE, version: str = "v1"
) -> LLMRequest:
    return LLMRequest(
        step=step,
        messages=(Message(role="user", content=content),),
        schema_name="Thing",
        json_schema={"type": "object"},
        prompt_version=version,
        schema_version="v1",
    )


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------


def test_the_same_request_fingerprints_the_same() -> None:
    assert request_fingerprint(a_request()) == request_fingerprint(a_request())


@pytest.mark.parametrize(
    ("changed", "why"),
    [
        pytest.param({"content": "different"}, "message text", id="messages"),
        pytest.param({"step": LLMStep.SELECT}, "pipeline step", id="step"),
        pytest.param({"version": "v2"}, "prompt version", id="prompt-version"),
    ],
)
def test_anything_that_changes_the_answer_changes_the_fingerprint(
    changed: dict[str, object], why: str
) -> None:
    """A replay must not serve yesterday's answer to a different question."""
    assert request_fingerprint(a_request()) != request_fingerprint(a_request(**changed))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


async def test_a_recorded_reply_is_returned_verbatim() -> None:
    provider = OfflineLLMProvider()
    request = a_request()
    provider.record(request, '{"ok": true}')

    response = await provider.complete(request)

    assert response.content == '{"ok": true}'
    assert response.provider == "offline"
    assert response.model_served == OFFLINE_MODEL_ID


async def test_replay_is_stable_across_calls_within_what_was_recorded() -> None:
    """Determinism is the whole point of `rebuild --from-journal`."""
    provider = OfflineLLMProvider()
    request = a_request()
    provider.record(request, '{"n": 1}')
    provider.record(request, '{"n": 1}')

    first = await provider.complete(request)
    second = await provider.complete(request)

    assert first.content == second.content
    assert first.generation_id == second.generation_id


async def test_replay_beyond_what_was_recorded_fails_rather_than_repeating() -> None:
    """A pipeline that drifted must not silently receive a fabricated answer.

    Repeating the last recorded reply for a call the original run never made
    would let a changed pipeline use output that was never actually produced -
    exactly the failure a replay exists to rule out.
    """
    provider = OfflineLLMProvider()
    request = a_request()
    provider.record(request, '{"n": 1}')
    await provider.complete(request)

    with pytest.raises(LLMError, match="never fabricates"):
        await provider.complete(request)


async def test_an_unrecorded_request_fails_rather_than_inventing() -> None:
    """The property that makes an offline run trustworthy."""
    provider = OfflineLLMProvider()

    with pytest.raises(LLMError, match="never invents"):
        await provider.complete(a_request())


async def test_a_responder_supplies_replies_for_unseen_requests() -> None:
    provider = OfflineLLMProvider(responder=lambda request: f'{{"step": "{request.step}"}}')

    response = await provider.complete(a_request())

    assert response.content == '{"step": "organize"}'


async def test_a_recorded_reply_wins_over_the_responder() -> None:
    """Replay must beat generation, or a rebuild would not reproduce."""
    provider = OfflineLLMProvider(responder=lambda _: '{"from": "responder"}')
    request = a_request()
    provider.record(request, '{"from": "recording"}')

    assert (await provider.complete(request)).content == '{"from": "recording"}'


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_it_reports_no_strict_schema_support() -> None:
    """It models the weaker contract on purpose.

    A pipeline proven against it also works against a provider with no
    server-side schema enforcement, so a validation bug cannot hide until it
    reaches a real model.
    """
    assert OfflineLLMProvider().supports_strict_schema is False


async def test_calls_are_observable_for_assertions() -> None:
    provider = OfflineLLMProvider(responder=lambda _: "{}")

    await provider.complete(a_request(step=LLMStep.SELECT))
    await provider.complete(a_request(step=LLMStep.ORGANIZE))

    assert [c.step for c in provider.calls] == [LLMStep.SELECT, LLMStep.ORGANIZE]


async def test_usage_is_reported_so_budget_plumbing_stays_exercised() -> None:
    provider = OfflineLLMProvider(responder=lambda _: '{"x": 1}')

    response = await provider.complete(a_request())

    assert response.input_tokens is not None and response.input_tokens > 0
    assert response.output_tokens is not None and response.output_tokens > 0
    assert response.cost_usd is None, "an offline run costs nothing and must not claim otherwise"
