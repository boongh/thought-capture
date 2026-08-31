"""OpenRouter adapter, via the OpenAI-compatible client.

The OpenAI client is used rather than OpenRouter's own SDK deliberately: local
models later implement this same port through an OpenAI-compatible server such
as Ollama or vLLM, so the portable client is the one worth depending on
(docs/DESIGN.md 11).
"""

from __future__ import annotations

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
        params: dict[str, Any] = {
            "model": self._model_id,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }

        # Only sent when the pinned model actually supports it. Sending
        # response_format to a model that does not can be rejected outright by
        # provider routing, turning a working call into an error.
        if self._strict:
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.json_schema,
                },
            }

        started = time.perf_counter()
        try:
            completion = await self._client.chat.completions.create(**params)
        except APITimeoutError as exc:
            raise LLMError(f"{self._model_id} timed out after {REQUEST_TIMEOUT_SECONDS}s") from exc
        except APIStatusError as exc:
            # Status and model only. A provider error body can echo the prompt,
            # which for this system is raw thought text.
            raise LLMError(f"{self._model_id} returned HTTP {exc.status_code}") from exc
        except (APIError, httpx.HTTPError) as exc:
            raise LLMError(f"{self._model_id} call failed: {type(exc).__name__}") from exc

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
