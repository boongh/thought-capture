# ADR-0006: OpenRouter behind a provider port, with safe and custom model-selection modes

- **Status:** Accepted
- **Date:** 2026-08-31 (request-side provider routing added 2026-09-01)
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

A follow-up review of the Slice 9 PR went further: even with a reviewed model
pinned, the adapter sent no OpenRouter *provider-routing* controls at all.
OpenRouter's own documented defaults permit provider fallback and allow data
collection
(<https://openrouter.ai/docs/guides/routing/provider-selection>,
<https://openrouter.ai/docs/guides/features/zdr>) - so a request for a
reviewed model could still be silently served by an unreviewed backup
provider, or by one that trains on the request, and nothing in the adapter
said otherwise. The first draft of this ADR named that gap and deliberately
left it open rather than claim it was covered; this revision closes it.

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

**Request-side provider routing.** The startup allowlist governs which model
gets *asked for*; it says nothing about what OpenRouter does with that
request once sent. Every `OpenRouterProvider.complete` call now includes an
OpenRouter `provider` routing object (sent via the OpenAI SDK's `extra_body`,
since `provider` is not a field the SDK's typed client knows about), built
from two flags on the provider itself - `allow_fallbacks` and
`deny_data_collection` - which `Settings` computes from mode:

- **Safe mode**: `openrouter_allow_fallbacks` is always `False` and
  `openrouter_deny_data_collection` is always `True`, regardless of any other
  setting. A model earns a place in `REVIEWED_MODELS` for *its own*
  retention/schema behavior; whatever OpenRouter might substitute it with
  under fallback was never reviewed at all, so fallback is refused outright
  rather than restricted to other reviewed models. (The latter was
  considered - populate an OpenRouter model-fallback list from
  `REVIEWED_MODELS` - and rejected for now: with exactly one reviewed model,
  it is currently indistinguishable from no fallback, and doing it properly
  means depending on OpenRouter's model-array fallback contract, which hasn't
  been verified. Worth revisiting once the registry has more than one entry.)
  `data_collection: "deny"` is sent so the *request* enforces what the
  registry review already established, rather than only trusting the
  registry's own bookkeeping.
- **Custom mode**: `openrouter_allow_fallbacks` follows a new setting,
  `model_allow_fallback` (default `true`, matching OpenRouter's own default -
  a host who wants stricter behavior even in custom mode turns it off).
  `openrouter_deny_data_collection` is always `False` in custom mode: the
  `provider` object simply omits `data_collection` rather than sending
  `"allow"` explicitly, so OpenRouter's own account-level default applies.
  Forcing `"deny"` here would silently break the one documented custom-mode
  use case in this ADR - `nvidia/nemotron-3.5-lightning:free`, which requires
  accepting training/publishing to use at all.

`OpenRouterProvider` itself has no notion of "mode" - it only takes the two
flags, defaulting to the conservative values (`allow_fallbacks=False`,
`deny_data_collection=True`) so a direct construction that forgot to wire
`Settings` fails safe rather than silently permissive.

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

**Positive.** Closes the specific gap the Slice 9 follow-up review named: a
reviewed model pinned in safe mode can no longer be silently served by an
unreviewed fallback provider or one that retains the request, because the
adapter now says so on every request rather than only checking after the
fact.

**Negative.** Safe mode's model allowlist is a startup-time check; it cannot
verify that a reviewed model's provider hasn't changed its policy since
review. The request-side routing flags added in this revision narrow that
gap for *fallback and retention* specifically, but `model_served` diverging
from `model_requested` is still caught only after the call returns - that
remains `OpenRouterProvider`'s separate, unchanged job.

**Negative.** Every new model requires a code change to adopt in safe mode.
This is deliberate friction, not an oversight: it is what makes "reviewed"
mean something more than "someone typed a slug into `.env`".

**Deferred.** Nothing in this ADR yet wires `OpenRouterProvider` into a
composition root - no `apps/*` process constructs one today. When that
composition lands, it should read `model_selection_mode`, `REVIEWED_MODELS`,
`openrouter_allow_fallbacks`, and `openrouter_deny_data_collection` from the
same `Settings` object this ADR extends, so the startup allowlist, the
request-side routing flags, and the post-call served-model guard all stay
backed by one source of truth rather than drifting apart.

## Verification

`tests/unit/test_settings.py` asserts: a reviewed slug is accepted in safe
mode; an unreviewed slug is rejected in safe mode with a message naming
`docs/adr/0006`; the same unreviewed slug is accepted once
`TC_MODEL_SELECTION_MODE=custom` is set; an empty slug is accepted in safe
mode regardless (it selects the offline adapter, not a provider);
`openrouter_allow_fallbacks` is `False` in safe mode even when
`model_allow_fallback=true`, and follows the flag in custom mode; and
`openrouter_deny_data_collection` is `True` in safe mode and `False` in
custom mode unconditionally.

`tests/unit/test_openrouter_provider.py` asserts the `provider` object
actually sent (via `extra_body`, since `AsyncCompletions.create` has no typed
`provider` parameter): the conservative defaults produce
`{"allow_fallbacks": false, "data_collection": "deny"}`; allowing fallback
without denying data collection sends `allow_fallbacks: true` with no
`data_collection` key at all, rather than an explicit `"allow"`; and the
separate served-model guard this ADR does not change is still covered.
