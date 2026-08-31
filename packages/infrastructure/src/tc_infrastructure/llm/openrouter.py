"""OpenRouter adapter, via the OpenAI-compatible client.

The OpenAI client is used rather than OpenRouter's own SDK deliberately: local
models later implement this same port through an OpenAI-compatible server such
as Ollama or vLLM, so the portable client is the one worth depending on
(docs/DESIGN.md 11).
"""

from __future__ import annotations

import json
import logging
import time
from decimal import Decimal
from typing import Any

import httpx
from openai import APIError, APIStatusError, APITimeoutError, AsyncOpenAI

from tc_domain.llm import LLMError, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 120.0

# Sent for attribution. Deliberately carries no personal identifier
# (docs/DESIGN.md 11).
APP_TITLE = "Thought Capture AI"
APP_URL = "https://github.com/boongh/thought-capture"


def schema_instruction(request: LLMRequest) -> str:
    """The schema, rendered for a model that cannot be given it structurally."""
    return (
        f"Reply with a single JSON object matching the {request.schema_name} schema below. "
        "Return only the JSON: no prose, no explanation, no code fences.\n\n"
        f"{json.dumps(request.json_schema, indent=2, sort_keys=True)}"
    )


class OpenRouterProvider:
    """One pinned model behind OpenRouter."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model_id: str,
        supports_strict_schema: bool,
        client: AsyncOpenAI | None = None,
    ) -> None:
        if not model_id:
            raise ValueError("a pinned model slug is required; refusing a floating default")
        self._model_id = model_id
        self._strict = supports_strict_schema
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            # Retries are the caller's business: every attempt must be
            # journaled, and a silent SDK retry would not be.
            max_retries=0,
            default_headers={"HTTP-Referer": APP_URL, "X-OpenRouter-Title": APP_TITLE},
        )

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def supports_strict_schema(self) -> bool:
        return self._strict

    async def complete(self, request: LLMRequest) -> LLMResponse:
        messages = [{"role": m.role, "content": m.content} for m in request.messages]

        if self._strict:
            # The provider enforces the schema server-side.
            response_format: dict[str, Any] | None = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.json_schema,
                },
            }
        else:
            # A non-strict model must still be told the shape it has to
            # produce, or it is being asked to guess - and the repair prompt
            # refers to "the required JSON schema" it was never shown.
            # `response_format` is deliberately not sent: provider routing can
            # reject a model that does not advertise support, turning a working
            # call into an error.
            response_format = None
            messages.append({"role": "system", "content": schema_instruction(request)})

        params: dict[str, Any] = {
            "model": self._model_id,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }
        if response_format is not None:
            params["response_format"] = response_format

        started = time.perf_counter()
        # Each failure is re-raised `from None`, deliberately. Chaining with
        # `from exc` keeps the original SDK exception as __cause__, and
        # `logger.exception` renders the whole chain - including OpenAI status
        # error bodies, which can echo the provider's copy of the prompt. For
        # this system that is raw thought text, so the sanitized message must
        # be the only thing that can reach a log.
        try:
            completion = await self._client.chat.completions.create(**params)
        except APITimeoutError:
            logger.warning(
                "llm.timeout",
                extra={"model_requested": self._model_id, "timeout_s": REQUEST_TIMEOUT_SECONDS},
            )
            raise LLMError(f"{self._model_id} timed out after {REQUEST_TIMEOUT_SECONDS}s") from None
        except APIStatusError as exc:
            status = exc.status_code
            logger.warning(
                "llm.status_error", extra={"model_requested": self._model_id, "status": status}
            )
            raise LLMError(f"{self._model_id} returned HTTP {status}") from None
        except (APIError, httpx.HTTPError) as exc:
            kind = type(exc).__name__
            logger.warning(
                "llm.transport_error", extra={"model_requested": self._model_id, "kind": kind}
            )
            raise LLMError(f"{self._model_id} call failed: {kind}") from None

        latency_ms = int((time.perf_counter() - started) * 1000)
        raw = completion.model_dump()

        choices = raw.get("choices") or []
        content = ""
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
        if not content:
            raise LLMError(f"{self._model_id} returned an empty completion")

        usage = raw.get("usage") or {}
        cost = usage.get("cost")

        logger.info(
            "llm.completed",
            extra={
                "step": str(request.step),
                "model_requested": self._model_id,
                "model_served": raw.get("model"),
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "latency_ms": latency_ms,
            },
        )

        return LLMResponse(
            content=content,
            raw=raw,
            latency_ms=latency_ms,
            model_requested=self._model_id,
            # Recorded rather than assumed: a gateway may route elsewhere.
            model_served=raw.get("model"),
            provider=raw.get("provider"),
            generation_id=raw.get("id"),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cost_usd=Decimal(str(cost)) if cost is not None else None,
            request_params={
                "temperature": request.temperature,
                "max_tokens": request.max_output_tokens,
                "strict_schema": self._strict,
            },
        )
