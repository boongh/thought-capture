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

``qwen/qwen3.8-flash`` was the one candidate staged in ``env.example`` during
initial setup, but was removed on 2026-09-01: its only OpenRouter endpoint
(provider tag ``alibaba``) does not appear on OpenRouter's zero-data-retention
endpoint list (``openrouter.ai/api/v1/endpoints/zdr``), and Alibaba's own FAQ
does not clearly distinguish raw API-traffic retention (unconfirmed) from
console session-history retention (confirmed retained) - so it did not
actually meet the **private** bar above, despite having been admitted while
that gap was still recorded as merely "unconfirmed" rather than "checked and
failed."

``upstage/solar-pro4`` is the first real entry, reviewed for the ``select``
step (docs/DESIGN.md 7.3.2) across five rounds of live evaluation - see
``docs/model-evaluation-organize-select.md``. It is the only select candidate
tested with zero confirmed safety failures: on both the subtle
(false-authority/exfiltration) and blunt (textbook override) injection
probes it explicitly reasoned about the attempt and refused it, while still
correctly resolving the legitimate reference underneath. A round-3 rerun
surfaced a reproducible degenerate/looping-output bug under the blunt probe
(2/2); round 5's 5x rerun of that same probe shape came back clean 5/5, with
no recurrence. Reviewed provider: ``upstage/zdr``, confirmed live against
OpenRouter's endpoints API (``GET /api/v1/models/upstage/solar-pro4/endpoints``)
during round 5.

The ``organize`` step (docs/DESIGN.md 7.4) remains unreviewed. Round 4's
evaluation recommended ``meta-llama/llama-4-maverick``, but round 5's
higher-N rerun of its fabrication-B probe found a reproducible (3/3) defect
- generated document bodies containing only the ``## Summary`` heading, with
no content and none of the other three required sections - that left the
probe's actual question (does it invent an unstated connection) unanswered.
That candidate is not added here pending further evidence or an owner
decision; see "What this does not settle" in the evaluation document.
Organize therefore still runs on the deterministic offline adapter
(``Settings.uses_offline_model_adapter``). Custom mode exists precisely for
operating with a model that has not been through this review, at the host's
own risk.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReviewedModel:
    model_id: str
    supports_strict_schema: bool
    providers: frozenset[str]
    note: str


_ENTRIES: tuple[ReviewedModel, ...] = (
    ReviewedModel(
        model_id="upstage/solar-pro4",
        supports_strict_schema=True,
        providers=frozenset({"upstage/zdr"}),
        note=(
            "Reviewed for select only (docs/DESIGN.md 7.3.2), not organize. "
            "Zero confirmed safety failures across 5 evaluation rounds "
            "(docs/model-evaluation-organize-select.md): explicitly refuses "
            "both subtle and blunt injection probes while still resolving "
            "the legitimate reference. Round 3 saw a reproducible "
            "degenerate/looping-output bug under the blunt probe (2/2); "
            "round 5's 5x rerun of the same probe shape came back clean "
            "5/5."
        ),
    ),
)

REVIEWED_MODELS: dict[str, ReviewedModel] = {entry.model_id: entry for entry in _ENTRIES}
