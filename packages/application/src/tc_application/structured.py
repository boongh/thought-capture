"""Structured model output: validate, repair once, then fail loudly.

"One repair attempt may include validation errors; a second failure aborts the
stage" (docs/DESIGN.md 7.4). That rule is the whole contract. A pipeline that
retries indefinitely turns a broken prompt into an unbounded bill, and one that
accepts partial output writes unsourced claims into the canonical corpus.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from tc_domain.llm import (
    LLMError,
    LLMOutputInvalid,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMStep,
    Message,
)

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

# One call, then at most one repair. Not configurable: the bound is the point.
MAX_ATTEMPTS = 2

REPAIR_INSTRUCTION = (
    "Your previous reply did not satisfy the required JSON schema. "
    "Return only valid JSON matching the schema. Do not explain the error, "
    "do not wrap the JSON in prose or code fences, and do not invent any fact "
    "that was not present in the sources you were given.\n\n"
    "Validation errors:\n{errors}"
)

# The journal records every attempt, including the failed one, because ADR-0008
# reproduction depends on knowing what was actually sent and returned.
JournalWriter = Callable[[LLMRequest, LLMResponse, int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class StructuredResult[ModelT: BaseModel]:
    value: ModelT
    attempts: int
    repaired: bool
    responses: tuple[LLMResponse, ...]

    @property
    def input_tokens(self) -> int:
        return sum(r.input_tokens or 0 for r in self.responses)

    @property
    def output_tokens(self) -> int:
        return sum(r.output_tokens or 0 for r in self.responses)


def extract_json(content: str) -> str:
    """Recover the JSON object from a reply that may be wrapped.

    Models without server-side schema enforcement routinely return fenced code
    blocks or a sentence of preamble. Rejecting those outright would burn the
    single repair attempt on a formatting habit rather than a real error, so
    the obvious wrappers are stripped first.
    """
    text = content.strip()

    if text.startswith("```"):
        without_open = text.split("\n", 1)[-1] if "\n" in text else ""
        closing = without_open.rfind("```")
        text = (without_open[:closing] if closing != -1 else without_open).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


async def complete_structured[ModelT: BaseModel](
    provider: LLMProvider,
    request: LLMRequest,
    model: type[ModelT],
    *,
    journal: JournalWriter | None = None,
) -> StructuredResult[ModelT]:
    """Call the provider and parse its reply into ``model``.

    On the first validation failure the model is asked once more, with the
    specific errors quoted back. A second failure raises ``LLMOutputInvalid``.
    """
    attempt_request = request
    responses: list[LLMResponse] = []
    last_errors = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await provider.complete(attempt_request)
        except LLMError as exc:
            # A call that never produced output is still an attempt that was
            # made, and may have been paid for. ADR-0008 requires every attempt
            # to be recorded, so the failure is journaled before it propagates;
            # otherwise `llm_calls.error_code` could never be populated.
            if journal is not None:
                await journal(attempt_request, _failure_response(provider, exc), attempt)
            raise

        responses.append(response)
        if journal is not None:
            await journal(attempt_request, response, attempt)

        try:
            payload = json.loads(extract_json(response.content))
        except json.JSONDecodeError as exc:
            last_errors = f"the reply was not valid JSON: {exc}"
        else:
            try:
                value = model.model_validate(payload)
            except ValidationError as exc:
                last_errors = _summarize(exc)
            else:
                if attempt > 1:
                    logger.info("llm.repaired", extra={"step": str(request.step)})
                return StructuredResult(
                    value=value,
                    attempts=attempt,
                    repaired=attempt > 1,
                    responses=tuple(responses),
                )

        if attempt < MAX_ATTEMPTS:
            logger.warning(
                "llm.invalid_output",
                extra={"step": str(request.step), "attempt": attempt},
            )
            attempt_request = _repair_request(request, response, last_errors)

    raise LLMOutputInvalid(
        f"{request.schema_name} was not produced after {MAX_ATTEMPTS} attempts: {last_errors}",
        attempts=MAX_ATTEMPTS,
    )


def _failure_response(provider: LLMProvider, error: LLMError) -> LLMResponse:
    """A journalable record of a call that never returned output.

    The message is the adapter's already-sanitized text, never a provider error
    body, which can echo the prompt.
    """
    return LLMResponse(
        content="",
        raw={"error": str(error)},
        latency_ms=0,
        model_requested=provider.model_id,
        error_code=type(error).__name__,
    )


def _repair_request(original: LLMRequest, previous: LLMResponse, errors: str) -> LLMRequest:
    """Re-ask, quoting the model's own reply and the specific failures back."""
    return replace(
        original,
        step=LLMStep.REPAIR,
        messages=(
            *original.messages,
            Message(role="assistant", content=previous.content),
            Message(role="user", content=REPAIR_INSTRUCTION.format(errors=errors)),
        ),
    )


def _summarize(error: ValidationError) -> str:
    """Field paths and messages only.

    Pydantic includes the offending input in its default rendering, which for
    this system is raw thought text. That must not travel into a prompt echo or
    a log line (docs/DESIGN.md 14.2).
    """
    lines = []
    for item in error.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in item["loc"]) or "<root>"
        lines.append(f"- {location}: {item['msg']}")
    return "\n".join(lines)
