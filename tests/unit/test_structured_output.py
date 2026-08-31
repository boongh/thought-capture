"""Structured output: validate, repair once, fail loudly.

"One repair attempt may include validation errors; a second failure aborts the
stage" (docs/DESIGN.md 7.4). The bound is the contract: unbounded retries turn
a broken prompt into an unbounded bill, and accepting partial output writes
unsourced claims into the canonical corpus.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from tc_application.structured import (
    MAX_ATTEMPTS,
    complete_structured,
    extract_json,
)
from tc_domain.llm import LLMOutputInvalid, LLMRequest, LLMResponse, LLMStep, Message
from tc_infrastructure.llm.offline import OfflineLLMProvider


class ProposedDocument(BaseModel):
    stable_key: str
    title: str = Field(min_length=1, max_length=120)
    source_thought_ids: list[int] = Field(min_length=1)


VALID = {"stable_key": "project:x", "title": "Project X", "source_thought_ids": [1, 2]}


def a_request(step: LLMStep = LLMStep.ORGANIZE) -> LLMRequest:
    return LLMRequest(
        step=step,
        messages=(Message(role="user", content="organize these thoughts"),),
        schema_name="ProposedDocument",
        json_schema={"type": "object"},
        prompt_version="v1",
        schema_version="v1",
    )


class ScriptedProvider:
    """Returns a prepared reply per attempt, so repair behaviour is observable."""

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.requests: list[LLMRequest] = []

    @property
    def model_id(self) -> str:
        return "scripted/model"

    @property
    def supports_strict_schema(self) -> bool:
        return False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        content = self._replies[min(len(self.requests) - 1, len(self._replies) - 1)]
        return LLMResponse(
            content=content,
            raw={"content": content},
            latency_ms=1,
            model_requested=self.model_id,
            input_tokens=10,
            output_tokens=5,
        )


# ---------------------------------------------------------------------------
# Unwrapping
# ---------------------------------------------------------------------------


def test_plain_json_passes_through() -> None:
    assert json.loads(extract_json(json.dumps(VALID))) == VALID


def test_a_fenced_code_block_is_unwrapped() -> None:
    """Models without server-side enforcement routinely fence their JSON.

    Burning the single repair attempt on a formatting habit rather than a real
    error would waste the budget the design allows.
    """
    fenced = f"```json\n{json.dumps(VALID)}\n```"
    assert json.loads(extract_json(fenced)) == VALID


def test_surrounding_prose_is_discarded() -> None:
    chatty = f"Sure! Here is the result:\n\n{json.dumps(VALID)}\n\nLet me know."
    assert json.loads(extract_json(chatty)) == VALID


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_valid_output_is_returned_without_repair() -> None:
    provider = ScriptedProvider(json.dumps(VALID))

    result = await complete_structured(provider, a_request(), ProposedDocument)

    assert result.value.stable_key == "project:x"
    assert result.attempts == 1
    assert result.repaired is False
    assert len(provider.requests) == 1


async def test_usage_is_summed_across_attempts() -> None:
    """Budget accounting must include the failed attempt, which was still paid for."""
    provider = ScriptedProvider("not json", json.dumps(VALID))

    result = await complete_structured(provider, a_request(), ProposedDocument)

    assert result.attempts == 2
    assert result.input_tokens == 20
    assert result.output_tokens == 10


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


async def test_invalid_json_is_repaired_once() -> None:
    provider = ScriptedProvider("this is not json at all", json.dumps(VALID))

    result = await complete_structured(provider, a_request(), ProposedDocument)

    assert result.repaired is True
    assert result.attempts == 2


async def test_schema_violations_are_repaired_once() -> None:
    """Well-formed JSON that violates the schema still gets one more chance."""
    missing_sources = json.dumps({"stable_key": "project:x", "title": "Project X"})
    provider = ScriptedProvider(missing_sources, json.dumps(VALID))

    result = await complete_structured(provider, a_request(), ProposedDocument)

    assert result.repaired is True
    assert result.value.source_thought_ids == [1, 2]


async def test_the_repair_quotes_the_specific_errors_back() -> None:
    """A generic "try again" wastes the attempt; the model needs the failure."""
    provider = ScriptedProvider(json.dumps({"stable_key": "x"}), json.dumps(VALID))

    await complete_structured(provider, a_request(), ProposedDocument)

    repair = provider.requests[1]
    instruction = repair.messages[-1].content
    assert "source_thought_ids" in instruction
    assert "title" in instruction


async def test_the_repair_echoes_the_models_own_reply() -> None:
    bad = json.dumps({"stable_key": "x"})
    provider = ScriptedProvider(bad, json.dumps(VALID))

    await complete_structured(provider, a_request(), ProposedDocument)

    roles = [m.role for m in provider.requests[1].messages]
    assert "assistant" in roles
    assert any(m.content == bad for m in provider.requests[1].messages)


async def test_the_repair_attempt_is_labelled_as_such() -> None:
    """So the journal shows which calls were corrections rather than first tries."""
    provider = ScriptedProvider("nonsense", json.dumps(VALID))

    await complete_structured(provider, a_request(), ProposedDocument)

    assert provider.requests[0].step == LLMStep.ORGANIZE
    assert provider.requests[1].step == LLMStep.REPAIR


async def test_validation_errors_never_echo_the_offending_input() -> None:
    """Pydantic includes the input by default; here that is raw thought text.

    It must not travel into a prompt echo or a log line (docs/DESIGN.md 14.2).
    """
    secret = "a private thought that must not be echoed"
    provider = ScriptedProvider(
        json.dumps({"stable_key": secret, "title": "", "source_thought_ids": []}),
        json.dumps(VALID),
    )

    await complete_structured(provider, a_request(), ProposedDocument)

    instruction = provider.requests[1].messages[-1].content
    assert secret not in instruction


# ---------------------------------------------------------------------------
# Failing loudly
# ---------------------------------------------------------------------------


async def test_a_second_failure_aborts() -> None:
    """Never a third attempt, and never a partial result."""
    provider = ScriptedProvider("nonsense", "still nonsense", json.dumps(VALID))

    with pytest.raises(LLMOutputInvalid) as caught:
        await complete_structured(provider, a_request(), ProposedDocument)

    assert caught.value.attempts == MAX_ATTEMPTS
    assert len(provider.requests) == MAX_ATTEMPTS


async def test_the_failure_names_the_schema_that_could_not_be_produced() -> None:
    provider = ScriptedProvider("nonsense", "still nonsense")

    with pytest.raises(LLMOutputInvalid, match="ProposedDocument"):
        await complete_structured(provider, a_request(), ProposedDocument)


# ---------------------------------------------------------------------------
# Journaling
# ---------------------------------------------------------------------------


async def test_every_attempt_is_journaled_including_the_failure() -> None:
    """ADR-0008 reproduction depends on the failed attempt being recorded too."""
    provider = ScriptedProvider("nonsense", json.dumps(VALID))
    recorded: list[tuple[LLMStep, int]] = []

    async def journal(request: LLMRequest, response: LLMResponse, attempt: int) -> None:
        recorded.append((request.step, attempt))

    await complete_structured(provider, a_request(), ProposedDocument, journal=journal)

    assert recorded == [(LLMStep.ORGANIZE, 1), (LLMStep.REPAIR, 2)]


# ---------------------------------------------------------------------------
# Against the offline provider
# ---------------------------------------------------------------------------


async def test_the_offline_provider_replays_a_recorded_reply() -> None:
    provider = OfflineLLMProvider()
    request = a_request()
    provider.record(request, json.dumps(VALID))

    result = await complete_structured(provider, request, ProposedDocument)

    assert result.value.title == "Project X"
    assert result.attempts == 1
