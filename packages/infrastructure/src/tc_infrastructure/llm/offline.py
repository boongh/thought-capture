"""Deterministic offline provider.

Selected whenever no model slug is pinned, so an unconfigured deployment runs
the whole pipeline without silently calling a provider - and so the test suite
never depends on a network, a key, or a free-tier rate limit.

It has two modes, and the distinction is the point:

- **Replay** - return the exact response a previous run recorded for the same
  request. This is what makes ``rebuild --from-journal`` deterministic
  (ADR-0008).
- **Canned** - return a scripted response for a request never seen before.
  Used by tests to drive specific behaviour.

It is not a mock of a model. It never invents content, so a pipeline run
against it proves orchestration, validation, persistence, and provenance -
never generation quality.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable

from tc_domain.llm import LLMError, LLMRequest, LLMResponse

logger = logging.getLogger(__name__)

OFFLINE_MODEL_ID = "offline/deterministic"

Responder = Callable[[LLMRequest], str]


def request_fingerprint(request: LLMRequest) -> str:
    """A stable key for one request.

    Covers everything that could change the reply: the rendered messages, the
    schema, and both versions. Two runs that fingerprint the same are asking
    the same question, so replaying the recorded answer is honest.
    """
    material = json.dumps(
        {
            "step": str(request.step),
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "schema_name": request.schema_name,
            "json_schema": request.json_schema,
            "prompt_version": request.prompt_version,
            "schema_version": request.schema_version,
            "temperature": request.temperature,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class OfflineLLMProvider:
    """Serves recorded or scripted responses. Never reaches the network."""

    def __init__(
        self,
        *,
        recorded: dict[str, str] | None = None,
        responder: Responder | None = None,
        model_id: str = OFFLINE_MODEL_ID,
    ) -> None:
        self._recorded = dict(recorded or {})
        self._responder = responder
        self._model_id = model_id
        self.calls: list[LLMRequest] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def supports_strict_schema(self) -> bool:
        """False, deliberately.

        The offline provider models the *weaker* contract, so a pipeline proven
        against it also works against a provider with no server-side schema
        enforcement. Claiming strict support here would let a validation bug
        hide until it reached a real model.
        """
        return False

    def record(self, request: LLMRequest, content: str) -> None:
        self._recorded[request_fingerprint(request)] = content

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        fingerprint = request_fingerprint(request)

        content = self._recorded.get(fingerprint)
        source = "replay"
        if content is None and self._responder is not None:
            content = self._responder(request)
            source = "scripted"

        if content is None:
            raise LLMError(
                f"no recorded response for {request.schema_name} "
                f"(fingerprint {fingerprint[:12]}); the offline provider never invents output"
            )

        logger.info(
            "llm.offline",
            extra={"step": str(request.step), "source": source, "fingerprint": fingerprint[:12]},
        )

        return LLMResponse(
            content=content,
            # `content` is stored flat as well as returned, so a journalled
            # offline response can be replayed by the same reader that handles
            # an OpenAI-compatible completion.
            raw={
                "offline": True,
                "source": source,
                "fingerprint": fingerprint,
                "content": content,
            },
            latency_ms=0,
            model_requested=self._model_id,
            model_served=self._model_id,
            provider="offline",
            generation_id=f"offline-{fingerprint[:16]}",
            input_tokens=_estimate_tokens(request),
            output_tokens=max(1, len(content) // 4),
            cost_usd=None,
            request_params={"temperature": request.temperature},
        )


def _estimate_tokens(request: LLMRequest) -> int:
    """Rough, and labelled as such: used only to keep budget plumbing exercised."""
    return max(1, sum(len(m.content) for m in request.messages) // 4)
