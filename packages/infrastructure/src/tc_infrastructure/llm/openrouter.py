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
        allowed_served_models: frozenset[str] | None = None,
        allow_fallbacks: bool = False,
        deny_data_collection: bool = True,
        only_providers: frozenset[str] | None = None,
        require_zdr: bool = True,
    ) -> None:
        if not model_id:
            raise ValueError("a pinned model slug is required; refusing a floating default")
        self._model_id = model_id
        self._strict = supports_strict_schema
        # docs/DESIGN.md 11: "Disable silent fallback between materially
        # different models for organization unless the fallback model is
        # explicitly tested." Defaults to exact-match only; a deployment that
        # has tested and accepted specific alternates passes them explicitly.
        self._allowed_served_models = allowed_served_models or frozenset({model_id})
        # Request-side routing constraints (docs/adr/0006), independent of the
        # post-response served-model check above: that check catches a
        # gateway that substituted a model anyway; these ask it not to in the
        # first place, and not to route through a provider that retains or
        # trains on the request. Both default to the conservative setting -
        # no fallback, deny retention - so constructing this class directly
        # (a test, or a caller that forgot to wire mode-awareness) fails safe.
        # `docs/adr/0006` ties the *effective* values to safe/custom mode via
        # `Settings.openrouter_allow_fallbacks`/`openrouter_deny_data_collection`;
        # this class itself has no notion of "mode", only these two flags.
        self._allow_fallbacks = allow_fallbacks
        self._deny_data_collection = deny_data_collection
        # `allow_fallbacks: false` alone only refuses a *second* provider
        # after the first fails - it does not restrict which provider
        # OpenRouter picks first under its own default load-balancing.
        # `only_providers`, when given, is sent as `provider.only` to
        # restrict that initial choice to specific reviewed providers too.
        # `None` (the default) sends no restriction at all, which is not the
        # same as "safe": a caller in safe mode is expected to pass the
        # provider(s) actually recorded against the reviewed model, once
        # `ReviewedModel` carries that data.
        self._only_providers = only_providers
        # `data_collection: "deny"` filters providers by their declared
        # policy tag; `zdr: true` is a stricter, independent OpenRouter
        # request-routing constraint that restricts to providers on
        # OpenRouter's own verified zero-data-retention endpoint list
        # (https://openrouter.ai/docs/guides/features/zdr) - the two are not
        # equivalent, since a provider can retain requests operationally
        # (abuse monitoring, say) without "training" on them, pass
        # `data_collection: "deny"`, and still not be ZDR-listed
        # (docs/adr/0006). Defaults to `True`, same fail-safe rationale as
        # `deny_data_collection`.
        self._require_zdr = require_zdr
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

        # OpenRouter's own defaults permit provider fallback and allow
        # data collection (https://openrouter.ai/docs/guides/routing/provider-selection,
        # https://openrouter.ai/docs/guides/features/zdr) - neither is safe to
        # inherit silently for a system whose prompts are raw personal memory
        # content. `allow_fallbacks: false` refuses to route to a different
        # provider than the one actually vetted for `model_id`; omitting
        # `data_collection` rather than never sending it lets a caller that
        # deliberately wants OpenRouter's default (a custom-mode host on a
        # free, training-opted-in tier) actually get it - sending "allow"
        # explicitly would be us choosing that on their behalf.
        provider_routing: dict[str, Any] = {"allow_fallbacks": self._allow_fallbacks}
        if self._deny_data_collection:
            provider_routing["data_collection"] = "deny"
        if self._require_zdr:
            # Restricts routing to OpenRouter's confirmed zero-data-retention
            # endpoints specifically - a live, per-request check, unlike
            # `ReviewedModel.providers`/`only`, which is a claim recorded at
            # review time. Omitted rather than sent as `false` when not
            # required, matching `data_collection`'s omit-to-inherit
            # behavior: OpenRouter has no documented "zdr: false", only the
            # absence of the constraint.
            provider_routing["zdr"] = True
        if self._strict:
            # OpenRouter defaults `require_parameters` to false, meaning a
            # provider that does not actually support every parameter we send
            # - `response_format`'s strict JSON-schema enforcement, here -
            # can still be selected and silently ignore it rather than
            # failing loudly. `supports_strict_schema` is this class's own
            # promise that schema enforcement is load-bearing for this call;
            # `require_parameters: true` makes OpenRouter honor that promise
            # by excluding any provider that can't actually keep it.
            provider_routing["require_parameters"] = True
        if self._only_providers:
            # Restricts which provider OpenRouter may route to *initially*,
            # not just after a failure - `allow_fallbacks: false` alone only
            # blocks backup attempts once a provider is already selected, it
            # does not constrain that first selection under OpenRouter's
            # default load-balancing.
            provider_routing["only"] = list(self._only_providers)

        params: dict[str, Any] = {
            "model": self._model_id,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            # `provider` is an OpenRouter extension, not an OpenAI Chat
            # Completions field, so the typed `create()` call has no
            # parameter for it - `extra_body` is the OpenAI SDK's documented
            # escape hatch for vendor-specific request fields like this one.
            "extra_body": {"provider": provider_routing},
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

        served = raw.get("model")
        if served not in self._allowed_served_models:
            # Rejected before any content is used: a document derived from an
            # unapproved model is exactly the silent fallback docs/DESIGN.md 11
            # prohibits, and the mismatch is worth knowing about even though
            # the call otherwise succeeded.
            logger.warning(
                "llm.unapproved_model_served",
                extra={"model_requested": self._model_id, "model_served": served},
            )
            raise LLMError(
                f"{self._model_id} served unapproved model {served!r}; "
                "add it to allowed_served_models once it has been explicitly tested, "
                "or fix routing so the requested model is served"
            )

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
                # The routing/retention policy actually requested, not just
                # the model - a historical run needs this to establish what
                # was asked for, since REVIEWED_MODELS and Settings can both
                # change after the call that used them was journaled.
                "provider_routing": provider_routing,
            },
        )
