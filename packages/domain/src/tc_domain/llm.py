"""The model provider port and its value types.

Stdlib only, like the rest of the domain. Provider SDKs live behind
``tc_infrastructure`` so that switching from OpenRouter to a local
OpenAI-compatible server is configuration plus evaluation, not business-logic
work (docs/DESIGN.md 11).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from tc_domain.errors import DomainError


class LLMStep(StrEnum):
    """Matches the ``step`` CHECK constraint on ``llm_calls``."""

    SELECT = "select"
    ENTITY_EXTRACT = "entity_extract"
    ORGANIZE = "organize"
    QUERY_PLAN = "query_plan"
    REPAIR = "repair"


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """One model call.

    ``json_schema`` is always supplied. A provider that supports strict schema
    enforcement uses it; one that does not still receives it in the prompt, and
    the caller validates client-side either way (docs/DESIGN.md 7.4).
    """

    step: LLMStep
    messages: tuple[Message, ...]
    schema_name: str
    json_schema: dict[str, Any]
    prompt_version: str
    schema_version: str
    max_output_tokens: int = 8192
    # Determinism is requested, never assumed: output is not reproducible across
    # providers even at zero. Reproduction comes from the journal (ADR-0008).
    temperature: float = 0.0
    # Unset by default. A model with hidden reasoning tokens can otherwise
    # spend its entire output budget on reasoning and return no visible
    # content at all (observed live during organize model evaluation -
    # docs/model-evaluation-organize-select.md). "none" asks the provider to
    # skip reasoning; other values are accepted for future evaluation use but
    # no caller sets them yet. Provider-agnostic by design - `OpenRouterProvider`
    # is the only adapter that currently acts on it.
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """What the provider returned, plus what it cost.

    ``model_served`` and ``provider`` are recorded rather than assumed because
    a gateway may route a request elsewhere; docs/DESIGN.md 11 requires
    recording the model actually returned on every run.
    """

    content: str
    raw: dict[str, Any]
    latency_ms: int
    model_requested: str
    model_served: str | None = None
    provider: str | None = None
    generation_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: Decimal | None = None
    error_code: str | None = None
    request_params: dict[str, Any] = field(default_factory=dict)


class LLMError(DomainError):
    """The provider could not be reached, or refused the request."""


class LLMOutputInvalid(DomainError):
    """The model's output did not satisfy the schema after the repair attempt.

    Raised rather than swallowed: docs/DESIGN.md 14.1 requires invalid model
    JSON to fail loudly after one repair, never to be silently discarded or
    partially applied.
    """

    def __init__(self, message: str, *, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts


@runtime_checkable
class LLMProvider(Protocol):
    """A model gateway."""

    @property
    def model_id(self) -> str:
        """The pinned slug. Never a floating alias (docs/DESIGN.md 11)."""
        ...

    @property
    def supports_strict_schema(self) -> bool:
        """Whether the provider enforces ``json_schema`` server-side.

        False means the schema is guidance only and client-side validation is
        the sole guarantee. The organize pipeline must behave correctly either
        way, so this changes cost and reliability, never correctness.
        """
        ...

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Issue one call.

        Raises ``LLMError`` on transport, authentication, or provider failure.
        Must not retry internally: retry policy and its journaling belong to the
        caller, so that every attempt is recorded.
        """
        ...
