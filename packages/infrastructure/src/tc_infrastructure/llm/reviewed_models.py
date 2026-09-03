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

The ``organize`` step (docs/DESIGN.md 7.4) was unreviewed through round 7.
Round 4's evaluation recommended ``meta-llama/llama-4-maverick``, but round
5's higher-N rerun of its fabrication-B probe found a reproducible (3/3)
defect - generated document bodies containing only the ``## Summary``
heading, with no content and none of the other three required sections -
that left the probe's actual question (does it invent an unstated
connection) unanswered. **The owner decided (2026-09-02) not to pursue it
further: ruled out for organize.** Round 6 tried one fresh,
previously-untested-for-organize candidate, ``ibm-granite/granite-4.2-8b``
(``coreweave/bf16``) - also ruled out, on different grounds: both
fabrication probes (N=1 each) consumed the full 8192-token completion
budget in a decoding/repetition loop and never produced parseable JSON
(~78-80s latency, ~$0.0013/call), so the fabrication question itself
couldn't be evaluated. See "Round 6" in
``docs/model-evaluation-organize-select.md``.

Rounds 8-11 found ``x-ai/grok-4.3`` (``xai/zdr``), the first organize
candidate to pass every probe across an N=5 confirmation - see that entry
above for the full evidence summary. Round 12 tried a newer sibling
(``x-ai/grok-build-0.1``) as a possible upgrade; it asserted the
fabrication-A planted claim as confirmed fact in one section despite
hedging it in another, a worse safety showing than ``grok-4.3`` at higher
cost, and was not adopted. Round 13 re-searched the reasoning-model
landscape broadly (26 candidates, $0.04/M-$2.5/M token price range,
including major-lab alternatives Claude Haiku 4.5, and OpenAI/Gemini
candidates blocked by a ZDR-routing gap before a single probe could run)
specifically looking for something cheaper than ``grok-4.3`` that also
clears the fabrication bar - none did; every candidate failed on
structural/degenerate output, runaway decoding, mandatory uncontrollable
reasoning, or an unhedged fabrication assertion, the last of which held
even for the best-aligned major-lab candidate tested. The owner ended the
search there and approved wiring ``x-ai/grok-4.3`` in
(2026-09-03): it is now the reviewed organize candidate. Custom mode still
exists for operating with a model that has not been through this review, at
the host's own risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# The pipeline stage(s) a reviewed slug for TC_MODEL_ORGANIZE/TC_MODEL_SELECT/
# TC_MODEL_QUERY_PLAN may be pinned to (docs/DESIGN.md 7.3.2, 7.4). A model's
# review is stage-specific: the safety/injection probes, prompt shape, and
# cost profile that earned it a place here were run against one step's actual
# task, not the others - see e.g. upstage/solar-pro4 (select-only) and
# x-ai/grok-4.3 (organize-only) below. `Settings._safe_mode_restricts_to_reviewed_models`
# rejects a slug pinned to a field whose stage is not in this set, even if the
# slug is REVIEWED_MODELS-adjacent for a different stage.
Stage = Literal["organize", "select", "query_plan"]


@dataclass(frozen=True, slots=True)
class ReviewedModel:
    model_id: str
    supports_strict_schema: bool
    providers: frozenset[str]
    stages: frozenset[Stage]
    note: str
    # Unset for a non-reasoning reviewed model (e.g. upstage/solar-pro4).
    # Recorded here, not left to an operator-set flag, for the same reason
    # `supports_strict_schema` is: a reasoning model's safe cost profile is
    # itself a reviewed fact, not a mutable knob - see
    # `x-ai/grok-4.3`'s entry below and Settings.reasoning_effort_for.
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None


_ENTRIES: tuple[ReviewedModel, ...] = (
    ReviewedModel(
        model_id="upstage/solar-pro4",
        supports_strict_schema=True,
        providers=frozenset({"upstage/zdr"}),
        stages=frozenset({"select"}),
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
    ReviewedModel(
        model_id="x-ai/grok-4.3",
        supports_strict_schema=True,
        providers=frozenset({"xai/zdr"}),
        stages=frozenset({"organize"}),
        reasoning_effort="none",
        note=(
            "Reviewed for organize only (docs/DESIGN.md 7.4), not select. "
            "Strongest organize candidate across 13 evaluation rounds "
            "(docs/model-evaluation-organize-select.md): N=5-confirmed clean "
            "on the fabrication-B (invented-link/entity-mis-binding) probe, "
            "best-in-class zero injection-marker compliance on the blunt "
            "probe across every rep, and never asserted the fabrication-A "
            "planted claim as confident settled fact the way every "
            "ruled-out candidate did (round 11-confirm). The one structural "
            "gap - schema-invalid output from a missing daily_digest "
            "document on the fabrication-A/blunt probes - is confirmed "
            "recovered by production's own repair loop "
            "(tc_application.structured.complete_structured) on attempt 2, "
            "safety intact both times; not a capability gap. "
            "reasoning_effort='none' is required, not optional: round 8 "
            "found it cuts realized cost ~40-65% vs. leaving reasoning "
            "uncontrolled with safety/quality unchanged, and the worst-case "
            "monthly cost at the uncontrolled 'low' level would exceed the "
            "project's $0.25/month operational cap outright. Round 13 "
            "re-searched the full reasoning-model landscape (26 additional "
            "candidates, every price tier from $0.04/M to $2.5/M tokens) "
            "for anything cheaper that also clears the fabrication bar - "
            "none did; this remains the only organize candidate reviewed."
        ),
    ),
)

REVIEWED_MODELS: dict[str, ReviewedModel] = {entry.model_id: entry for entry in _ENTRIES}
