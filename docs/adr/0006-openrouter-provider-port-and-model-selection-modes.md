# ADR-0006: OpenRouter behind a provider port, with safe and custom model-selection modes

- **Status:** Accepted
- **Date:** 2026-08-31 (request-side provider routing added 2026-09-01; provider-endpoint pinning and `require_parameters` added 2026-09-01; `qwen/qwen3.8-flash` removed from the registry 2026-09-01)
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
left it open rather than claim it was covered; that revision closed data
collection and *backup* fallback specifically.

A second review pass on that revision (same day) found it still incomplete:
`allow_fallbacks: false` only refuses a *second* provider once OpenRouter has
already picked and failed over from a first one - it does nothing to
constrain that first, initial pick, which OpenRouter makes under its own
default load-balancing across whatever providers currently serve the pinned
model. `data_collection: "deny"` filters by policy but is not the same thing
as an owner-reviewed allowlist of specific providers. The reviewer also noted
that OpenRouter defaults `require_parameters` to `false`, so a provider that
does not actually honor every parameter in the request - the strict
JSON-schema enforcement, specifically - can be selected anyway and silently
drop it rather than failing loudly. This revision closes both.

The system is Release-1 single-owner, self-hosted (`docs/DESIGN.md` 1). The
owner is also the only operator. So the guard this ADR adds is not protecting
one party from another; it is protecting a rushed `.env` edit from silently
overriding a decision the design already asks the owner to make deliberately.

A third review pass questioned the registry's one entry itself, not just the
request-side controls around it: `qwen/qwen3.8-flash`'s only endpoint
(provider tag `alibaba`) does not appear on OpenRouter's own
zero-data-retention endpoint list
(`openrouter.ai/api/v1/endpoints/zdr`, checked 2026-09-01), and the registry's
own note already recorded that Alibaba's raw API-traffic retention (distinct
from console session-history retention, which it does retain) was never
independently confirmed. `data_collection: "deny"` and `provider.only` are
real request-side controls, but they enforce *routing*, not the underlying
provider's actual retention behavior - and the registry had admitted this
entry while that behavior was still an open question rather than a checked
"no". That is a failure of the **private** admission bar this ADR itself
defines, not a gap the routing controls can close. The owner's decision:
remove the entry rather than keep it as an accepted-risk exception. See
"Provider-endpoint pinning" and the Decision/Consequences sections below for
what the registry contains now.

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

  A fourth field, `ReviewedModel.providers`, records which specific
  OpenRouter provider endpoint(s) were checked - not just the model. See
  "Provider-endpoint pinning" below for why this is a separate field rather
  than folded into the model-review criteria above.

  The registry is currently empty - `qwen/qwen3.8-flash`, the research
  `env.example` originally staged, was added and then removed the same day
  once its endpoint failed the private bar (see Context). With an empty
  registry, a non-empty `model_organize`/`model_query_plan` in safe mode is
  always rejected, so organize/query-plan run on the deterministic offline
  adapter until a model actually passes review. Adding a model means adding
  an entry after doing the review above - a code change and a commit, not a
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

The full `provider` routing object actually sent is also recorded in
`LLMResponse.request_params`, which the `llm_calls` journal (`docs/adr/0008`)
persists verbatim with every call, not just the temperature/max-tokens/
strict-schema fields already there - `REVIEWED_MODELS` and `Settings` can
both change after a call is journaled, so reconstructing "what routing
policy was actually in effect for this historical run" from current config
would be unreliable.

**Provider-endpoint pinning.** `allow_fallbacks: false` refuses a *second*
provider after the first fails; it does not restrict OpenRouter's *first*
choice, made under its own default load-balancing across every current
endpoint for the pinned model. `OpenRouterProvider` gains a third flag,
`only_providers: frozenset[str] | None`, sent as `provider.only` when set -
this restricts the initial choice too, to specific provider endpoints. `None`
(the default) sends no restriction, which is not itself "safe": a caller in
safe mode is expected to pass `ReviewedModel.providers` for the pinned model.

`qwen/qwen3.8-flash` was researched via OpenRouter's endpoints API
(`openrouter.ai/api/v1/models/qwen/qwen3.8-flash/endpoints`, checked
2026-09-01) as the mechanism's first real example: exactly one endpoint
existed, provider tag `alibaba`, confirmed to support `require_parameters`
with `response_format`/structured outputs. It no longer appears in the
registry - checking OpenRouter's zero-data-retention endpoint list the same
day showed that endpoint was not on it, which meant the raw-API-traffic
retention question the entry's note had already flagged as unconfirmed was
actually an unconfirmed "no", not an open "maybe". The mechanism itself
(`provider.only`, sourced from `ReviewedModel.providers`) is unchanged and
still exists for whichever model is added next.

Custom mode never sends `provider.only`: `Settings.openrouter_only_providers`
returns `None` unconditionally outside safe mode, even for a model slug that
happens to also appear in `REVIEWED_MODELS` - custom mode's promise is
freedom from the allowlist machinery entirely, not a silent partial
application of it.

**`require_parameters`.** OpenRouter defaults this to `false`, so a provider
that does not actually honor the parameters in the request - the strict
`response_format` enforcement, specifically - can still be selected and
silently ignore it rather than failing loudly. `OpenRouterProvider` now sends
`require_parameters: true` whenever `supports_strict_schema` is set, since
that flag is this class's own promise that schema enforcement is load-bearing
for the call; there is nothing to require for the non-strict, prompted path.

## Consequences

**Positive.** The gap between "the design says review the model" and "the
code enforces it" closes. An operator who leaves the default in place cannot
accidentally pin an unreviewed or training-opted-in model; they have to
explicitly choose custom mode, which is a deliberate, greppable signal in
their own `.env` that they are outside the reviewed set.

**Positive.** The reviewed-models registry is the one place this project's
actual position on "is this model acceptable" lives in code, instead of only
in a comment. It is small on purpose - Release 1 needs exactly one working
model, not a curated marketplace - and, as of 2026-09-01, currently empty:
removing an entry that failed review is the registry doing its job, not a
regression.

**Positive.** Closes the specific gap the Slice 9 follow-up review named: a
reviewed model pinned in safe mode can no longer be silently served by an
unreviewed fallback provider or one that retains the request, because the
adapter now says so on every request rather than only checking after the
fact.

**Positive.** Closes the second-pass finding too: the *initial* provider
choice is now restricted to reviewed endpoints (`provider.only`), not just
backup attempts after a failure, and a provider that can't actually enforce
the schema it's asked for is excluded outright (`require_parameters`) rather
than silently serving a best-effort approximation.

**Negative.** Safe mode's model allowlist is a startup-time check; it cannot
verify that a reviewed model's provider hasn't changed its policy since
review. The request-side routing flags narrow that gap for *fallback,
retention, and initial provider choice* specifically, but `model_served`
diverging from `model_requested` is still caught only after the call
returns - that remains `OpenRouterProvider`'s separate, unchanged job.

**Negative.** `ReviewedModel.providers` is a claim recorded at review time,
not a live check - if OpenRouter's endpoint list for a reviewed model changes
after review (an endpoint is deprecated, or the provider's retention policy
changes) and no one revisits the entry, `provider.only` simply makes every
safe-mode request for that model fail rather than silently falling through to
an unreviewed endpoint. That is the intended failure direction, but it does
mean an accepted entry is not a permanent guarantee.

**Negative.** With the registry empty, safe mode - the default - has no
provider-backed model to organize or answer queries with; both run on the
deterministic offline adapter until a model is added. This is the direct
cost of treating "private" as a checked fact rather than a plausible one:
Release 1's safe-mode LLM path is blocked on finding a model with a
confirmed-eligible endpoint, not merely a plausible one.

**Negative.** Every new model requires a code change to adopt in safe mode.
This is deliberate friction, not an oversight: it is what makes "reviewed"
mean something more than "someone typed a slug into `.env`".

**Deferred.** Nothing in this ADR yet wires `OpenRouterProvider` into a
composition root - no `apps/*` process constructs one today. When that
composition lands, it should read `model_selection_mode`, `REVIEWED_MODELS`,
`openrouter_allow_fallbacks`, `openrouter_deny_data_collection`, and
`openrouter_only_providers(model_id)` from the same `Settings` object this
ADR extends, so the startup allowlist, the request-side routing flags, and
the post-call served-model guard all stay backed by one source of truth
rather than drifting apart.

**Deferred.** Finding a model that is confirmed to meet all three admission
bars - safe, private (ideally via a confirmed OpenRouter zero-data-retention
endpoint, `provider.zdr`, not just a "deny" data-collection tag - see
Context) - and JSON-structure enforceable, so safe mode has a working
provider-backed path again. `provider.zdr` itself is not yet sent by
`OpenRouterProvider` at all; adopting it, and confirming its actual
undocumented failure behavior when no eligible endpoint exists, is separate
follow-up work, not assumed safe by this ADR.

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

`tests/unit/test_settings.py` additionally asserts `openrouter_only_providers`
returns `ReviewedModel.providers` for a reviewed slug in safe mode, `None` for
an unrecognized slug, and `None` unconditionally in custom mode even for a
slug that is also reviewed.

`tests/unit/test_openrouter_provider.py` asserts the `provider` object
actually sent (via `extra_body`, since `AsyncCompletions.create` has no typed
`provider` parameter): the conservative defaults produce
`{"allow_fallbacks": false, "data_collection": "deny", "require_parameters":
true}` (the last because the test provider defaults to strict-schema);
allowing fallback without denying data collection sends `allow_fallbacks:
true` with no `data_collection` key at all, rather than an explicit
`"allow"`; `require_parameters` is present for a strict-schema call and
absent for a non-strict one; `only_providers` produces `provider.only` when
given and is absent by default; the journaled `request_params["provider_routing"]`
matches the object actually sent; and the separate served-model guard this
ADR does not change is still covered.

`tests/unit/test_reviewed_models.py` asserts every registry entry records at
least one provider, that the registry is currently empty (documenting why,
so a future entry is a deliberate change to that test rather than a silent
diff), and guards `ReviewedModel`'s field shape with a synthetic entry
independent of the registry's actual contents. Because the real registry is
empty, `tests/unit/test_settings.py`'s safe-mode-accepts/restricts-providers
tests inject their own synthetic `REVIEWED_MODELS` entry via `monkeypatch`
rather than depend on a specific model having been reviewed.
