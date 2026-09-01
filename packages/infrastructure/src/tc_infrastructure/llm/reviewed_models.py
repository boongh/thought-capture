"""Models the owner has explicitly reviewed for safe-mode use.

Safe mode (``docs/adr/0006``) restricts model selection to the slugs listed
here. A model earns a place in this dict only after being checked against all
three of docs/DESIGN.md 11's model-selection concerns:

- **safe** - observed to treat captured text as quoted data rather than
  instructions, under the same "no tool access, prompt injection is expected"
  assumption as docs/DESIGN.md 12.2.
- **private** - the provider's data-collection/training policy for this model
  has been checked, and the model does not require opting into "may train on
  inputs" or "may publish prompts" to use.
- **JSON structure enforceable** - the model has been confirmed to support
  OpenRouter's ``response_format: {"type": "json_schema", "strict": true}``
  rather than only a best-effort prompted schema.

A fourth field, ``providers``, records which specific OpenRouter provider
endpoint(s) were actually reviewed for that model - not just the model
itself. ``allow_fallbacks: false`` alone only blocks a *second* provider
after the first one fails; it does not restrict which provider OpenRouter
picks *first* under its own default load-balancing. ``providers`` is what
``OpenRouterProvider`` sends as ``provider.only`` to pin that initial choice
to a reviewed endpoint too - a model can be "reviewed" while a provider newly
added to serve it has not been.

This list is currently empty. ``qwen/qwen3.8-flash`` was the one candidate
staged in ``env.example`` during initial setup, but was removed on
2026-09-01: its only OpenRouter endpoint (provider tag ``alibaba``) does not
appear on OpenRouter's zero-data-retention endpoint list
(``openrouter.ai/api/v1/endpoints/zdr``), and Alibaba's own FAQ does not
clearly distinguish raw API-traffic retention (unconfirmed) from console
session-history retention (confirmed retained) - so it did not actually meet
the **private** bar above, despite having been admitted while that gap was
still recorded as merely "unconfirmed" rather than "checked and failed."
Until a model is found that is confirmed to meet all three bars, safe mode
has no eligible provider-backed model and organize/query-plan run on the
deterministic offline adapter (``uses_offline_model_adapter``). Custom mode
exists precisely for operating with a model that has not been through this
review, at the host's own risk.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReviewedModel:
    model_id: str
    supports_strict_schema: bool
    providers: frozenset[str]
    note: str


_ENTRIES: tuple[ReviewedModel, ...] = ()

REVIEWED_MODELS: dict[str, ReviewedModel] = {entry.model_id: entry for entry in _ENTRIES}
