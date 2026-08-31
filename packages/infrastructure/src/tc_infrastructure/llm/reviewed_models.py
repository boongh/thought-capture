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
    note: str


_ENTRIES = (
    ReviewedModel(
        model_id="qwen/qwen3.8-flash",
        supports_strict_schema=True,
        note=(
            "~$0.25/month at one run per day; supports strict JSON-schema "
            "structured outputs; no training opt-in required."
        ),
    ),
)

REVIEWED_MODELS: dict[str, ReviewedModel] = {entry.model_id: entry for entry in _ENTRIES}
