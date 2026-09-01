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

This list starts with the one candidate already researched during initial
setup (see ``env.example``'s OpenRouter section); custom mode exists
precisely for operating with a model that has not been through this review.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReviewedModel:
    model_id: str
    supports_strict_schema: bool
    providers: frozenset[str]
    note: str


_ENTRIES = (
    ReviewedModel(
        model_id="qwen/qwen3.8-flash",
        supports_strict_schema=True,
        providers=frozenset({"alibaba"}),
        note=(
            "~$0.25/month at one run per day; supports strict JSON-schema "
            "structured outputs; no training opt-in required. Exactly one "
            "OpenRouter endpoint as of 2026-09-01 (Alibaba - "
            "openrouter.ai/api/v1/models/qwen/qwen3.8-flash/endpoints, "
            "provider tag 'alibaba'), confirmed to support "
            "require_parameters with response_format/structured_outputs. "
            "Alibaba Cloud's own FAQ states it does not train on this data; "
            "whether it retains raw API traffic (distinct from console "
            "session history, which it does retain) was not confirmed - "
            "worth an explicit ToS check if that distinction matters before "
            "relying on it further."
        ),
    ),
)

REVIEWED_MODELS: dict[str, ReviewedModel] = {entry.model_id: entry for entry in _ENTRIES}
