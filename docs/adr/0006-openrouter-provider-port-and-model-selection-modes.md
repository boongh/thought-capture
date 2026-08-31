# ADR-0006: OpenRouter behind a provider port, with safe and custom model-selection modes

- **Status:** Accepted
- **Date:** 2026-08-31
- **Design anchor:** extends `docs/DESIGN.md` 11, 12.1, 12.2
- **First implemented in:** `packages/infrastructure/src/tc_infrastructure/llm/openrouter.py`, `packages/infrastructure/src/tc_infrastructure/config.py`

## Context

`docs/DESIGN.md` 11 already commits to a discipline for OpenRouter use: pin an
exact model slug, verify structured-output support before relying on it,
understand a model's retention/training policy before sending it prompts, and
disable silent fallback to an untested model. `env.example` staged this
decision explicitly - "PENDING OWNER DECISION - see docs/adr/0006 when
written" - next to two already-researched candidates that sit on opposite
sides of the line that discipline draws:

- `qwen/qwen3.8-flash` - supports OpenRouter's `response_format: json_schema`
  with `strict: true`, and requires no training/publishing opt-in. ~$0.25/month
  at one run per day.
- `nvidia/nemotron-3.5-lightning:free` - $0/month, but OpenRouter's free tier
  requires enabling account-wide "may train on inputs" / "may publish
  prompts", and the model does not support `response_format` at all.

Codex's review of the workspace-isolation remediation patch flagged the gap
this ADR closes: the adapter's only defense was checking `model_served`
*after* the call returned. That check is real and worth keeping - a gateway
can silently route to a different model than the one requested - but it
cannot undo having already sent raw thought content to whatever model was
actually served. A pinned-but-unreviewed slug, or an operator who copies the
free-tier option without reading the training-opt-in clause, currently has no
code-level guard between the config file and a live disclosure.

The system is Release-1 single-owner, self-hosted (`docs/DESIGN.md` 1). The
owner is also the only operator. So the guard this ADR adds is not protecting
one party from another; it is protecting a rushed `.env` edit from silently
overriding a decision the design already asks the owner to make deliberately.

## Decision

Model selection has two modes, set by `TC_MODEL_SELECTION_MODE` (`safe` by
default):

- **Safe mode.** `model_organize` and `model_query_plan` must each be either
  empty (selects the deterministic offline adapter) or a slug present in
  `tc_infrastructure.llm.reviewed_models.REVIEWED_MODELS`. A slug enters that
  registry only after being checked against all three of `docs/DESIGN.md`
  11's concerns:
  - **Safe** - observed to treat captured text as quoted data rather than
    instructions (`docs/DESIGN.md` 12.2's prompt-injection assumption).
  - **Private** - the provider's data-collection/training policy for this
    model has been checked, and using it does not require opting into "may
    train on inputs" or "may publish prompts".
  - **JSON structure enforceable** - confirmed to support
    `response_format: {"type": "json_schema", "strict": true}` server-side,
    not just a prompted best-effort shape.

  The registry starts with one entry, `qwen/qwen3.8-flash`, carrying forward
  the research `env.example` already staged. Adding a model means adding an
  entry after doing the review above - a code change and a commit, not a
  config edit, which is the point: the review has to have actually happened.

- **Custom mode.** `TC_MODEL_SELECTION_MODE=custom` lifts the allowlist. Any
  non-empty slug is accepted, including `nvidia/nemotron-3.5-lightning:free`
  or any other model the owner wants to try. Custom mode does not weaken the
  after-the-fact `model_served` check in `OpenRouterProvider` - that guard
  applies in both modes, since it defends against a different failure (the
  gateway substituting a model you didn't ask for at all) than the one safe
  mode addresses (asking for a model that was never vetted in the first
  place).

The check runs once, at process startup, as a `pydantic` cross-field
validator on `Settings` - the same fail-fast pattern already used for an
unknown IANA timezone. A misconfigured safe-mode deployment refuses to start
rather than disclosing anything on its first organize run.

## Consequences

**Positive.** The gap between "the design says review the model" and "the
code enforces it" closes. An operator who leaves the default in place cannot
accidentally pin an unreviewed or training-opted-in model; they have to
explicitly choose custom mode, which is a deliberate, greppable signal in
their own `.env` that they are outside the reviewed set.

**Positive.** The reviewed-models registry is the one place this project's
actual position on "is this model acceptable" lives in code, instead of only
in a comment. It is small on purpose: Release 1 needs exactly one working
model, not a curated marketplace.

**Negative.** Safe mode is a startup-time allowlist check, not a runtime
guarantee about what a gateway actually does with a request. It cannot verify
that a reviewed model's provider hasn't changed its policy since review, and
it says nothing about `model_served` diverging from `model_requested` - that
remains `OpenRouterProvider`'s job, unchanged by this ADR.

**Negative.** Every new model requires a code change to adopt in safe mode.
This is deliberate friction, not an oversight: it is what makes "reviewed"
mean something more than "someone typed a slug into `.env`".

**Deferred.** Nothing in this ADR yet wires `OpenRouterProvider` into a
composition root - no `apps/*` process constructs one today. When that
composition lands, it should read `model_selection_mode` and
`REVIEWED_MODELS` from the same `Settings` object this ADR extends, so the
two checks (startup allowlist, post-call served-model guard) stay backed by
one source of truth rather than drifting apart.

## Verification

`tests/unit/test_settings.py` asserts: a reviewed slug is accepted in safe
mode; an unreviewed slug is rejected in safe mode with a message naming
`docs/adr/0006`; the same unreviewed slug is accepted once
`TC_MODEL_SELECTION_MODE=custom` is set; and an empty slug is accepted in
safe mode regardless (it selects the offline adapter, not a provider).
`tests/unit/test_openrouter_provider.py` continues to cover the separate
served-model guard this ADR does not change.
