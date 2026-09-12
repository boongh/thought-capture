# Embedding sync — review round 4 fix plan (F15–F17)

Status: **plan only, not implemented**. Branch `feat/slice-2-embedding-sync`,
PR #36.
Input: Codex's independent evaluation of round 3, posted to PR #36 on
2026-09-12T07:22Z ([comment
5644441442](https://github.com/boongh/thought-capture/issues/36#issuecomment-5644441442)),
3 findings. Codex explicitly confirms round 3's F14 fix ("tokenization and encode
now run together inside the same admitted, semaphore-held operation") and does
**not** re-raise F13-A.
Rounds 1 (F1–F9), 2 (F10–F12) and 3 (F13–F14) are already committed on this
branch; see `docs/plans/embedding-sync-review-round-2.md` and
`docs/plans/embedding-sync-review-round-3.md`.

**One decision blocks part of this plan** (F15, §*Owner decision 1*). Wave A
(F16 + F17) is unblocked and can start immediately; Wave B (F15) cannot start
until that decision is made, because it changes accepted architecture and
therefore needs an ADR and the owner, never a fix agent.

This plan follows `.claude/skills/first-line-review/SKILL.md`: triage is done here
by the primary session (each finding re-derived from the code, not from Codex's
summary), fixes are delegated to parallel `fix-implementer` agents with disjoint
file ownership, verification is serial and orchestrator-owned, and the fix diff is
then re-reviewed by fresh `code-reviewer` + `integration-reviewer` agents. Nothing
is "done" until the validation gate in *Definition of done* is met in full.

---

## 0. Scope: which open PRs this plan covers

| PR | Head branch | Codex review? | CI | This plan |
|----|-------------|---------------|----|-----------|
| #36 | `feat/slice-2-embedding-sync` | **yes — 3 findings, 2026-09-12** | green | **in scope** (F15–F17) |
| #35 | `chore/agent-tooling-setup` | none posted | green | out of scope — see below |
| #34 | `docs/adr-0010-addendum-decision-b` | none posted | green | out of scope — see below |
| #33 | `fix/slice-1-followups` | none posted | green | out of scope — see below |

Codex has posted blocking findings on **PR #36 only**. #33–#35 carry no review of
any kind, so there is nothing to "resolve" on them; raising speculative work
against them here would be scope creep, not planning. Two facts about them matter
to this plan anyway and are recorded so they are not rediscovered mid-wave:

- **#35 is already contained in #36.** `git diff --stat main...feat/slice-2-embedding-sync`
  includes `.claude/skills/first-line-review/**`, `.claude/agents/*.md`, `CLAUDE.md`
  and `docs/plans/khoj-retirement-completion.md` — #35's entire diff. Merging #35
  first keeps the history honest; merging #36 first silently subsumes it. Owner's
  call, but it should be a deliberate one.
- **#33 edits `apps/embedding_sidecar/src/tc_embedding_sidecar/settings.py`
  (+88/−21).** F16 below changes how that same module's `model_id`/`model_revision`
  reach the container. The two do not overlap textually today, but whichever lands
  second must re-read the other's version of that file rather than assume. Flagged
  as a merge-order risk, not a blocker.
- **#33 touches sidecar code and `docs/DESIGN.md`; #34 is an ADR addendum.** Both
  fall in CLAUDE.md's "requires Codex's independent evaluation before merge"
  categories (deployment surface, accepted architecture). Recommend requesting one
  before merging either. That request is an owner action, not part of this plan.

---

## 1. Triage result

Each finding was re-derived from the code in this session, not accepted from the
report's summary.

| id | Codex finding | severity | verdict | disposition |
|----|---------------|----------|---------|-------------|
| F15 | No tracked/gated `runs.kind='reembed'` lifecycle exists | High | `CONFIRMED` as fact; **disposition is an owner decision, not a fix** | Wave B, 1 agent — *blocked on Owner decision 1* |
| F16 | Sidecar Compose service never forwards `TC_EMBEDDING_MODEL_ID`/`_REVISION`; model is baked at build time | Medium | `CONFIRMED`, **and broader than reported** | Fix now (Wave A, agent 1) |
| F17 | `HttpEmbeddingClient` silently coerces malformed `truncated` values | Medium | `CONFIRMED`, **two distinct defects in one block** | Fix now (Wave A, agent 2) |

Facts established during triage, recorded so they are not re-derived later:

- **`runs` already supports the `reembed` lifecycle. No migration is needed.**
  `migrations/versions/0008_document_embeddings.py:60-70` already lists
  `'reembed'` in `_RUNS_KIND_VALUES_BEFORE`, and
  `packages/infrastructure/src/tc_infrastructure/db/tables.py:102-126` already
  carries `runs.embedding_model_id`. This materially lowers F15's cost and is the
  single most important input to Owner decision 1.
- **Nothing reads `document_embeddings` yet** (unchanged since round 3: only
  `embedding_writer.py` touches the table; `search_reader.py` has no pgvector
  path). So the half of Codex's finding 1 that says "gate semantic reads through
  completion" has **no call site to gate**. It cannot be built or proved in this
  PR; it can only be recorded as a blocking acceptance criterion on the slice that
  first reads the table.
- **Codex's finding 1 names the wrong endpoint as the fix site.** It reports that
  `POST /v1/admin/embedding-sync` "does not create or reconcile `runs.kind='reembed'`".
  `docs/DESIGN.md:842` defines that endpoint *as* an enqueue-only force sync that
  "returns the count enqueued, not a synchronous embed", and deliberately shares
  one code path with the organize writer's per-revision enqueue. Making it open a
  `reembed` run would contradict DESIGN 10. The correct shape is a **separate
  `POST /v1/admin/reembed`**. The finding is right about the missing lifecycle and
  wrong about where it belongs; building it as reported would have introduced a
  new design conflict while closing this one.
- **F16 is not fixed by forwarding the variables.** `apps/embedding_sidecar/Dockerfile:59-63`
  bakes the weights at **build** time by importing `get_settings()` with no build
  args, and `:69` then sets `HF_HUB_OFFLINE=1`. Runtime-only forwarding converts a
  silent no-op into a container that crashes at model load. Both halves must move
  together or the fix is worse than the bug.
- **No new test suite is introduced by this plan.** Every new test extends a file
  an existing check-script step already collects (`tests/unit/`, `tests/integration/`,
  `apps/embedding_sidecar/tests/`). So `scripts/check.sh` / `scripts/check.ps1`
  need no extension. Stated explicitly because CLAUDE.md requires extending both
  scripts whenever a new harness *is* added; the verification step re-confirms this
  rather than assuming it. **F15 is the exception to watch** — if it lands a new
  `tests/integration/test_reembed_run.py`, that path is already collected by the
  integration marker, but the agent must confirm rather than assume.
- **Known pre-existing local failure.** `tests/unit/test_backup_run_id_collision.py`
  fails on this dev machine for an unrelated `date`-stub PATH quirk (recorded in
  round 3). It is not this round's regression; it must still be reported, never
  silently absorbed.

---

## F15 — The tracked, gated `reembed` lifecycle (High)

### What Codex actually found

There is no `runs.kind='reembed'` row, no recorded target model, no progress
reconciliation, and no read gate. `docs/DESIGN.md:800` (8.5) and `:815` (9.2) both
require that "changing the embedding model requires a `reembed` run
(`runs.kind = 'reembed'`, `runs.embedding_model_id`) that recomputes every
`document_embeddings` row before the new model is read from". That is true, and it
is true *by design decision*: this is verbatim **F13-B**, which the owner
considered and deferred on 2026-09-11 (`docs/plans/embedding-sync-review-round-3.md:197-215`,
and again at `:481` decision 1).

So this is not a newly discovered bug. It is Codex and the owner disagreeing about
whether a recorded deferral is acceptable. Under
`.claude/skills/first-line-review/SKILL.md` §3 that is an **owner decision** —
"correct behavior is genuinely ambiguous, or the fix would change accepted
architecture. That needs an ADR and the owner, never a fix agent." Under CLAUDE.md
it is a source conflict, which must be surfaced rather than silently resolved.

### Current failure envelope (what is and is not exposed today)

With F13-A landed (`66ae5d8`), a model change cannot *corrupt* anything: the
writer raises `EmbeddingModelConflictError`, the worker logs
`embedding_sync.model_conflict` at ERROR, and the events retry then dead-letter.
Semantic sync halts loudly. Combined with "nothing reads the table yet", the
residual exposure is:

1. a model migration is an **untracked** manual procedure — no run row, no target
   model recorded, no way to ask "is the rebuild finished?" other than counting
   rows by hand (`docs/OPERATING.md:243-290`);
2. the procedure's step 3 tells an operator to `DELETE FROM document_embeddings`
   by hand, which is exactly the kind of un-audited destructive step
   `docs/incidents/0001` exists to stop repeating;
3. and — per F16 below — **the procedure does not currently work at all**, because
   the model change it is premised on never reaches the container.

Point 3 is why F16 is not merely the smaller finding: F15's runbook and F16's
plumbing describe the same operator action, and today neither end of it is real.

### Owner decision 1 — required before Wave B starts

> **Does the tracked `reembed` lifecycle land in PR #36, or is the deferral
> formally recorded as an ADR-0010 addendum?**

**Option 1 — implement F15-A now (recommended).** Build the trackable half inside
this PR; record the read gate as a blocking acceptance criterion on the read slice
via an ADR-0010 addendum.

- *For:* no migration needed (established above); it closes Codex's real complaint
  — an untracked, hand-run model migration — inside the PR that created the
  hazard; it replaces the hand-written `DELETE` with an audited, transactional
  endpoint; and it is bounded work, roughly one vertical slice across five files
  plus tests.
- *Against:* it widens a diff that is already 5,181 lines and has been through
  three review rounds. That is a real cost, and the skill's own stopping rules name
  "the diff has grown materially past the original slice" as a reason to stop.

**Option 2 — formally defer, with an ADR addendum.** Keep F13-A + the corrected
runbook as the accepted interim posture; land the lifecycle with the read slice.

- *For:* smallest diff; matches the owner's existing recorded decision; the read
  gate genuinely cannot be built or tested today, so half the finding is
  unbuildable either way.
- *Against:* requires Codex to accept a *recorded* architectural deferral. A
  deferral that exists only in an untracked `docs/plans/` file is not an accepted
  decision — hence the addendum. PR #34 is already an ADR-0010 addendum branch, so
  the precedent and the mechanism both exist.

**Option 3 — build the complete lifecycle including read gating now.** Not
recommended and arguably not possible: the gate has no call site, so it would ship
as unexecuted code with no test that can fail. It also drags the search slice's
design decisions into this PR.

**Recommendation: Option 1**, with the addendum from Option 2 written anyway to
record what F15-B (read gating) must do. The two are not alternatives — Option 1
still needs the addendum for the half it cannot build. The owner is the final
authority here; nothing below starts until this is answered.

### Required change — F15-A (only if Option 1 is chosen)

A new, separate admin action. `POST /v1/admin/embedding-sync` is **not** modified —
DESIGN 10 defines it, and it stays as specified.

1. **Domain port** — `packages/domain/src/tc_domain/embedding_ports.py`: a
   `ReembedRunStore` protocol (`start`, `reconcile`, `find_running`) and a
   `ReembedRun` value object carrying `run_id`, `workspace_id`,
   `embedding_model_id`, `status`, `enqueued`. Plus a narrow `current_model_id()`
   on `EmbeddingPort` (see *Sub-decision* below).
2. **Application** — new `packages/application/src/tc_application/reembed.py`:
   - `StartReembedRun(workspace_id)` → in **one transaction**: insert
     `runs(kind='reembed', status='running', embedding_model_id=<target>, …)`,
     delete that workspace's `document_embeddings` rows, and enqueue one
     `embedding.sync_requested` per current document by **reusing**
     `enqueue_embedding_sync` (`packages/infrastructure/.../db/embedding_sync_enqueue.py`)
     so forced sync, organize-triggered sync and reembed all share one enqueue
     path. Acknowledgement (the HTTP response) only after that transaction
     commits — CLAUDE.md's durability invariant, and the reason the delete and the
     enqueue cannot be two calls.
   - `ReconcileReembedRuns()` → for each workspace with a `running` reembed run,
     mark `succeeded` + `finished_at` once `count(document_embeddings WHERE
     embedding_model_id = run.embedding_model_id)` equals the count of current
     documents; mark `failed` with `error_code` if any of that workspace's
     `embedding.sync_requested` events has exhausted `max_attempts`.
3. **Infrastructure** — new `packages/infrastructure/src/tc_infrastructure/db/reembed_run.py`
   implementing the port. The workspace scope must come from the `documents` join,
   as `embedding_writer.py` already does — `document_embeddings` has no
   `workspace_id` column of its own, and losing that join is precisely the
   scoping-at-a-handoff failure `integration-reviewer` exists to catch.
4. **API** — `apps/api/src/tc_api/routers/admin.py` gains `POST /v1/admin/reembed`;
   `apps/api/src/tc_api/schemas.py` gains `ReembedResponse{run_id, enqueued,
   embedding_model_id}`; `apps/api/src/tc_api/dependencies.py` wires the use case.
   Workspace from the auth context, never a query parameter (DESIGN 10).
5. **Worker** — `apps/worker/src/tc_worker/embedding_sync_loop.py` calls
   `ReconcileReembedRuns` after each poll cycle; `apps/worker/src/tc_worker/__main__.py`
   constructs it.
6. **Docs** — `docs/adr/0010-*.md` addendum: the lifecycle, and F15-B (read gating)
   as a blocking acceptance criterion on the read slice. `docs/OPERATING.md`'s
   model-conflict runbook replaced by the endpoint (keeping the backup step).
   `docs/DESIGN.md:842`'s endpoint table gains the new row.

**Sub-decision (agent must not guess): where does the target model id come from?**
The API process does not hold the sidecar's model id. Two workable shapes:

- *(a, recommended)* add `current_model_id()` to `EmbeddingPort` /
  `HttpEmbeddingClient`, reading the sidecar's `/health` (which already reports
  `model_id` and `model_revision`) and composing it through the existing
  `compose_embedding_model_id()` — the one place that join is allowed to happen.
  An unreachable sidecar then fails the request loudly (503) instead of recording
  a run against a guessed model.
- *(b)* require the operator to pass the target id in the request body and
  validate it against the sidecar. More ceremony, same failure modes, one more
  thing to get wrong by hand.

Take (a) unless the owner says otherwise; note it adds surface to `EmbeddingPort`,
which `integration-reviewer` should be pointed at explicitly.

### Proof (F15-A)

New `tests/integration/test_reembed_run.py`:

- start → a `runs` row exists with `kind='reembed'`, `status='running'`, the
  composed target `embedding_model_id`, and `started_at` set;
- start → every one of that workspace's `document_embeddings` rows is gone and
  exactly one `embedding.sync_requested` event exists per current document;
- **atomicity**: an enqueue failure injected mid-call leaves the run row absent
  *and* the embeddings intact — nothing half-applied;
- **workspace scoping**: a second workspace's embeddings and events are untouched;
- reconciliation marks `succeeded` only when every current document is embedded
  under the target model, and `failed` when an event dead-letters;
- a second `POST /v1/admin/reembed` while one is `running` is rejected (or is
  idempotent — state which in the brief; do not let the agent pick).

Extend `tests/integration/test_api_admin.py`: unauthenticated → 401; the response
shape; workspace comes from auth, not from a parameter.

New `tests/unit/test_reembed.py`: reconciliation state transitions against fakes,
and that no document body text reaches any log record.

Commands: `uv run pytest -m integration tests/integration/test_reembed_run.py
tests/integration/test_api_admin.py` (Docker required) and
`uv run pytest tests/unit/test_reembed.py`.

### Owned files (Wave B, single agent — deliberately not parallelised)

```
packages/domain/src/tc_domain/embedding_ports.py
packages/application/src/tc_application/reembed.py            (new)
packages/infrastructure/src/tc_infrastructure/db/reembed_run.py (new)
packages/infrastructure/src/tc_infrastructure/embedding/client.py   [only if sub-decision (a)]
apps/api/src/tc_api/routers/admin.py
apps/api/src/tc_api/schemas.py
apps/api/src/tc_api/dependencies.py
apps/worker/src/tc_worker/embedding_sync_loop.py
apps/worker/src/tc_worker/__main__.py
tests/integration/test_reembed_run.py                          (new)
tests/integration/test_api_admin.py
tests/unit/test_reembed.py                                     (new)
```

Out of bounds for the agent: `migrations/**` (none needed — if the agent believes
one is needed, that is a **stop-and-report**, not a migration it writes),
`docs/DESIGN.md`, `docs/adr/**`, `docs/OPERATING.md`, `deploy/compose/**`,
`env.example`, both check scripts, `pyproject.toml`, `uv.lock`. The orchestrator
writes every doc and ADR change itself, after the code lands, so the documentation
describes what was actually built.

**Why one agent, not three.** The port definitions in `embedding_ports.py` are the
shared contract between the application, infrastructure, API and worker edits.
Splitting a single vertical feature across parallel agents that must agree on a
contract none of them owns is exactly the contract-drift failure mode
`integration-reviewer` is chartered to find — manufacturing it on purpose to save
wall-clock time is a bad trade.

### Follow-ups F15 deliberately does not close

- **F15-B, read gating.** `degraded=true` (or an outright refusal) on semantic
  reads while a reembed run is `running`, plus the `embedding_model_id` read
  filter the owner already made a blocking criterion in round 3. Both belong to
  the slice that first reads `document_embeddings`. Carried into the ADR addendum
  so they are a criterion, not a memory.
- **Dimensionality changes.** A model whose output is not 384-dim still needs a
  migration on the `vector(384)` column. The lifecycle does not make model swaps
  free.

---

## F16 — A model change must actually reach the sidecar, or the runbook must stop claiming it does (Medium)

### Failure scenario (re-derived from the code)

`deploy/compose/docker-compose.yml:258-292` defines `embedding-sidecar` with
`build`, `image`, `profiles`, `healthcheck`, `mem_limit`, `cpus`, `security_opt`
and `networks` — and **no `environment:` block at all**. Compose never injects an
arbitrary `.env` key into a container on its own (the same fact that produced
round 2's F12 against the `worker` service). So:

1. An operator reads `docs/OPERATING.md:234`, which names
   `TC_EMBEDDING_MODEL_ID` / `TC_EMBEDDING_MODEL_REVISION` "in `.env`" as the
   cause of a model conflict — and reasonably concludes they are the lever.
   `packages/domain/src/tc_domain/embedding_ports.py:70-71` says the same.
2. They set one in `.env` and restart the stack.
3. `apps/embedding_sidecar/src/tc_embedding_sidecar/settings.py:22,34` still
   returns the built-in defaults, because nothing forwarded the override. The
   sidecar keeps embedding with `thenlper/gte-small@17e1f34…`. **Silent no-op.**
4. `env.example` documents neither variable (it documents six other
   `TC_EMBEDDING_*` keys, `:159-191`), so there is no third place to notice the
   discrepancy.

**And the naive fix is worse than the bug.** `apps/embedding_sidecar/Dockerfile:59-63`
bakes the weights at build time by importing `get_settings()` — with no `ARG`, so
it always bakes the *defaults* — and `:69` then sets `HF_HUB_OFFLINE=1`. Adding
only an `environment:` block would give a container whose settings name a model
whose weights are not in the image and whose hub access is disabled: a start-time
crash. Build-time and run-time values must move together.

The honest summary: the documented model-change procedure does not exist. That is
also why F15's runbook rewrite must land *after* this fix, not beside it.

### Required change

1. **`apps/embedding_sidecar/Dockerfile`** — add
   `ARG TC_EMBEDDING_MODEL_ID` / `ARG TC_EMBEDDING_MODEL_REVISION`, promoted to
   `ENV` **before** the bake step, with defaults identical to `settings.py:22,34`.
   The bake step keeps importing `get_settings()` — unchanged — so build-time and
   run-time read one value through one code path, which is the property the
   existing Dockerfile comment already claims and does not yet have.
   `HF_HUB_OFFLINE=1` stays exactly where it is: it is what turns a bake/run
   mismatch into a loud startup failure instead of a silent network fetch.
2. **`deploy/compose/docker-compose.yml`** — the `embedding-sidecar` service gains
   `build.args` **and** an `environment:` block, both referencing the same two
   variables with the same defaults, plus an anchor comment explaining why both
   are needed (mirroring the `worker` service's F12 comment at `:308-332`, which
   is the established house pattern for exactly this).
3. **`env.example`** — document both variables next to the other `TC_EMBEDDING_*`
   keys, stating plainly that a change requires rebuilding the sidecar image
   (`--build embedding-sidecar`) *and* a full re-embed, and pointing at the
   runbook. This is the file an operator actually edits; if it does not say
   "rebuild", the fix is only half done.
4. **`docs/OPERATING.md`** — correct the model-change runbook: the model is a
   build-time pin, here is the exact `docker compose … up -d --build
   embedding-sidecar` invocation, and here is where it sits relative to the
   re-embed. Do **not** write F15's endpoint into it in this wave — F16's agent
   describes only what F16 actually built; the orchestrator folds in F15's
   supersession afterwards.

### Proof (F16)

Extend `tests/unit/test_compose_embedding_env.py` (static inspection, no Docker —
the same cheap-regression-guard pattern the file already exists to apply) with a
sidecar class asserting:

- both variables appear in the `embedding-sidecar` service's `environment`;
- both appear in that service's `build.args`;
- the Compose defaults, the Dockerfile `ARG` defaults, and `settings.py`'s field
  defaults are all **the same three strings** — parsed from all three files, not
  hard-coded in the test. This is the assertion that actually prevents drift; the
  presence checks alone would pass a config that bakes one model and runs another.
- `env.example` documents both keys.

Extend `apps/embedding_sidecar/tests/test_settings.py`: `TC_EMBEDDING_MODEL_ID` /
`TC_EMBEDDING_MODEL_REVISION` in the environment actually override the defaults
(guards the `env_prefix` contract the whole fix rests on).

**Negative evidence is mandatory:** every new assertion must be confirmed to fail
against the current tree before the fix lands. A static test that never failed
proves nothing.

Commands: `uv run pytest tests/unit/test_compose_embedding_env.py` and
`cd apps/embedding_sidecar && uv run pytest tests/test_settings.py`.
Docker rebuild verification (`docker compose … up -d --build embedding-sidecar`
with an overridden revision, expecting a healthy container reporting the
overridden id on `/health`) is **orchestrator-only**, in the serial verify step —
fix agents never run `docker`.

### Owned files (Wave A, agent 1)

```
apps/embedding_sidecar/Dockerfile
deploy/compose/docker-compose.yml          [exclusive resource]
env.example                                [exclusive resource]
docs/OPERATING.md                          [exclusive this wave]
tests/unit/test_compose_embedding_env.py
apps/embedding_sidecar/tests/test_settings.py
```

Read-only for context: `apps/embedding_sidecar/src/tc_embedding_sidecar/settings.py`
(F16 must **not** edit it — PR #33 rewrites that file; see §0), the `worker`
service block in the same compose file, and
`deploy/compose/embedding-sidecar.contract-test.docker-compose.yml`.

Out of bounds: `scripts/check.sh`, `scripts/check.ps1` (no new suite is
introduced; if the agent concludes one is, stop and report), `docs/DESIGN.md`,
`migrations/**`, any `packages/**` or `apps/api|worker/**` source.

**This agent holds three of the skill's exclusive resources.** No other agent in
Wave A may touch `deploy/compose/*.yml`, `env.example`, or `docs/OPERATING.md`.
That constraint, not the grouping heuristic, is why F16 is one agent's whole job.

---

## F17 — `truncated` must be parsed, not coerced (Medium)

### Failure scenario (re-derived from the code)

`packages/infrastructure/src/tc_infrastructure/embedding/client.py:132-144`:

```python
truncated_flags: object = payload.get("truncated")
if truncated_flags is None:
    truncated_values: list[bool] = [False] * expected_count
else:
    ...
    truncated_values = [bool(flag) for flag in truncated_flags]
```

Two distinct defects share this block:

1. **Absent and present-null are conflated.** `payload.get("truncated")` returns
   `None` both when the key is missing (the intended backward-compatibility path
   for an older sidecar, per the module docstring at `:5-9`) and when the sidecar
   sends `{"truncated": null}` — a shape its own contract never produces. The
   second case takes the all-false path instead of raising.
2. **`bool(flag)` coerces anything.** `["false"]` → `True`. `[""]` → `False`.
   `[0]` → `False`. `[{}]` → `False`. A sidecar regression, a middlebox that
   stringifies JSON, or a future non-Python implementation all produce silently
   wrong truncation state.

Consequence today is observability, not corruption: `truncated` drives only the
`embedding_sync.truncated` INFO log
(`packages/application/src/tc_application/embedding_sync.py:85-94`). But it is the
one signal that says "this document's embedding does not represent the whole
document", and a signal that lies is worse than one that is absent — which is
exactly the `EmbeddingUnavailableError` contract this module's docstring already
promises ("a response shape that does not match what was asked for … is mapped to
`EmbeddingUnavailableError`"). The code does not currently keep that promise.

### Required change

- Replace `payload.get("truncated")` with a module-level unique sentinel
  (`_ABSENT = object()`) and `payload.get("truncated", _ABSENT)`. Only `_ABSENT`
  takes the all-false backward-compatibility path; an explicit `null` is a shape
  violation.
- Require every element to be an exact `bool`: `if not isinstance(flag, bool):
  raise ValueError(...)`. `isinstance(1, bool)` is `False`, so integers are
  correctly rejected without needing `type(...) is bool`.
- Keep the existing list-type and length checks and the enclosing
  `except (KeyError, TypeError, ValueError)` → `EmbeddingUnavailableError` wrapper
  at `:85-99` — the new `ValueError`s flow through it unchanged, so no caller
  contract moves.
- Correct the module docstring at `:5-9`: `truncated` is optional when **absent**,
  not when null.

### Proof (F17)

Extend `tests/unit/test_embedding_client.py`:

- key absent → every vector `truncated=False` (existing behavior, must not
  regress);
- `{"truncated": None}` → raises `EmbeddingUnavailableError` (**fails today**);
- parametrised `["false"]`, `[1]`, `[0]`, `[""]`, `[{}]`, `[None]` → each raises
  (`["false"]` and `[1]` **fail today**);
- `[True, False]` → exactly those values, in order;
- wrong-length list still raises (regression guard on the untouched check).

`tests/contract/embedding_sidecar/test_embedding_client.py` must pass
**unedited** — it runs against a real sidecar, and needing to edit it would mean
the wire contract moved, which this fix must not do.

Commands: `uv run pytest tests/unit/test_embedding_client.py`; contract suite via
the check script's own gate, orchestrator-run.

**Negative evidence is mandatory:** the three cases marked "fails today" must be
confirmed red against the pre-fix tree.

### Owned files (Wave A, agent 2)

```
packages/infrastructure/src/tc_infrastructure/embedding/client.py
tests/unit/test_embedding_client.py
```

Read-only for context: `packages/domain/src/tc_domain/embedding_ports.py`,
`packages/application/src/tc_application/embedding_sync.py`,
`apps/embedding_sidecar/src/tc_embedding_sidecar/schemas.py`,
`tests/contract/embedding_sidecar/test_embedding_client.py`.

Out of bounds: the sidecar's own `truncated` producer (it is correct; this is a
client-side parsing fix), and anything F15 or F16 owns. Disjoint from agent 1 by
construction.

---

## 2. Execution plan (subagents)

**Pre-flight (orchestrator, serial).**
Confirm no Codex session is editing this checkout — CLAUDE.md forbids two agents
in one working tree, and `feat/slice-1-pgvector-embedding-sidecar` is already
checked out in a separate worktree, so confirm which tree each process is in.
Confirm `uv` is on PATH and Docker is up; Codex's own round could not run
`./scripts/check.ps1` because `uv` was missing, and a round with that gap is not a
verified round. If either is unavailable, say so up front and treat the affected
stages as `NOT RUN` in the final report, never as passes. Write the review packet
(`git diff main...HEAD`, `--stat`, `--name-only`) into the **session scratchpad**,
never into the repo. Tell every agent that untracked paths
(`.agents/skills/first-line-review/`, `.codex/agents/*.toml`,
`docs/plans/embedding-sync-review-round-*.md`) are out of scope.

**Wave A — 2 `fix-implementer` agents, both spawned in a single message, in
parallel.**
Agent 1 → F16. Agent 2 → F17. File sets are disjoint; agent 1 alone holds the
exclusive resources. Each brief carries, verbatim: the re-derived failure
scenario, the root cause, the required change, the exact owned-file list, the
read-only list, the proof tests with their commands, the negative-evidence
requirement, and the hard rules — no git state changes, no `docker`, no `uv lock`,
no running either check script, targeted tests only, and **stop and report rather
than widen scope**. Agent 1's brief must state explicitly that forwarding the two
variables without the matching build args produces a container that crashes at
model load, and why.

**Owner gate.** Wave B does not start until Owner decision 1 is answered. If the
answer is Option 2, Wave B becomes an orchestrator-written ADR-0010 addendum plus
a runbook edit — no fix agent at all.

**Wave B — 1 `fix-implementer` agent (F15-A), only under Option 1, only after
Wave A has returned and verified.** Sequenced after Wave A because F15's runbook
supersedes the text F16 corrects, and because a half-verified tree is a bad base
for a vertical feature.

**Docs (orchestrator, serial, after each wave).**
`docs/adr/0010-*.md`, `docs/DESIGN.md`, and F15's supersession of the
`docs/OPERATING.md` runbook. Held back from every parallel wave for the standing
reason: documentation must describe what actually landed, not what was planned.
`docs/DESIGN.md`, `CLAUDE.md`, both check scripts, `deploy/compose/*.yml` and
`env.example` stay on the exclusive-resources list throughout.

**Verify (orchestrator, serial, exactly once per wave, never delegated).**

1. Read the complete `git diff` and confirm each agent stayed in its lane and
   produced the planned hunks, not adjacent opportunism.
2. Run `./scripts/check.ps1` once, in isolation. `PASS WITH SKIPS` is not a pass:
   every `NOT RUN` line goes into the report with its reason.
3. F16 additionally needs a **real rebuild check** the static tests cannot give:
   build the sidecar with an overridden `TC_EMBEDDING_MODEL_REVISION`, confirm the
   container comes up healthy and `/health` reports the overridden value, then
   rebuild back to the pinned default. Follow CLAUDE.md's Docker rules — read-only
   inspection (`docker compose -p thought-capture ps`, `docker volume ls`) in the
   same turn, never `down -v`, never an implicit project name, and use
   `scripts/compose-teardown.*` for any throwaway teardown.
4. Re-confirm no new test suite was introduced that either check script fails to
   collect.

**Re-review (3 agents, fresh, one message, in parallel, after each wave).**
Two `code-reviewer` shards plus one `integration-reviewer` over the whole fix diff
against `docs/DESIGN.md` 8.4/8.5, 9.2, 10 and ADR-0010. Never reuse an agent that
wrote a fix. Shards for Wave A: (a) `deploy/compose/docker-compose.yml` +
`apps/embedding_sidecar/Dockerfile` + `env.example` + `docs/OPERATING.md` + their
tests; (b) `embedding/client.py` + its tests + the sidecar schema it parses.
Point them specifically at: a compose default that disagrees with the Dockerfile
`ARG` default (the drift the static test is supposed to catch — check the test
actually would); `HF_HUB_OFFLINE` interacting badly with an overridden revision;
the runbook now describing a procedure that still does not work end to end; a
`truncated` shape the new checks still let through; and the `/embed` wire contract
having moved without anyone noticing. For Wave B, add a shard over the new
reembed vertical and direct the integration reviewer at workspace scoping through
the `documents` join, acknowledgement ordered after durable commit, and
`EmbeddingPort` growing a method its other implementations do not have.

**Decide.** Loop if any `CONFIRMED` fix-now finding survives orchestrator triage;
otherwise stop. Triage stays in the primary session — a subagent's finding becomes
a fix only after the primary session reads the underlying code and agrees. **This
is round 4.** The skill's stopping rule ("three rounds without converging → hand
it to the owner") is already live: if round 4 does not close, stop looping and
escalate rather than generating churn.

**Handoff.** One commit per finding, in CLAUDE.md's required change-report format,
on this branch — forward fixes only, no history rewrite, no force-push. Then
request **Codex's independent evaluation**, mandatory here: this touches
persistence, retrieval and the deployment surface.

---

## 3. Definition of done — the validation gate

The task is **not** finished when the code compiles, when the agents report
success, or when the targeted tests pass. All of the following must hold, and each
must be reported with its exact command and result:

1. Every test listed under *Proof* in each in-scope finding exists and passes.
2. Every test marked "fails today" was **confirmed red against the pre-fix tree**
   before the fix landed — F16's three-way default-equality assertion and F17's
   `null` / `["false"]` / `[1]` cases. A regression test that never failed proves
   nothing; running them against the pre-fix tree is part of the work.
3. F16's live rebuild check passed: an overridden revision produced a healthy
   container reporting the overridden value, and the stack was returned to the
   pinned default afterwards.
4. The sidecar contract suite passes **unedited** against a freshly built image.
5. `./scripts/check.ps1` ran to completion. Its unit, integration, contract,
   sidecar-lint, sidecar-types and sidecar-test stages all actually executed; any
   stage that did not run is named, with its reason, in the report. The known
   pre-existing `test_backup_run_id_collision.py` failure is reported as
   pre-existing, with evidence that it is untouched by this round — not absorbed.
6. The re-review round produced no surviving `CONFIRMED` must-fix finding.
7. Owner decision 1 is recorded in this file, and — whichever option was chosen —
   the ADR-0010 addendum capturing F15-B exists and names the read filter and the
   read gate as blocking acceptance criteria on the read slice.
8. Codex's independent evaluation has been requested with the complete diff and
   the verification evidence.

Report failures as failures, with output. A stage skipped because Docker or `uv`
was unavailable is a hole in the evidence and must be stated as one.

---

## 4. Owner decisions

| # | question | status |
|---|----------|--------|
| 1 | F15: implement the tracked `reembed` lifecycle in PR #36 (Option 1, recommended), or formally defer it via an ADR-0010 addendum (Option 2)? | **decided 2026-09-12: Option 1 (implement now)** |
| 2 | F15 sub-decision: target model id from a new `EmbeddingPort.current_model_id()` (recommended), or from a validated request body? | **decided 2026-09-12: `current_model_id()`** |
| 3 | F15: is a second `POST /v1/admin/reembed` while one is `running` rejected, or idempotent? | **decided 2026-09-12: idempotent** |
| 4 | Merge order: land #35 before #36 (honest history) or let #36 subsume it? | open — not blocking |
| 5 | Request Codex's independent evaluation on #33 (sidecar code + DESIGN.md) and #34 (ADR addendum) before merging them? | open — not blocking this branch |

Decisions 2–5 are cheap and can be answered alongside 1. Only decision 1 gates
work; Wave A (F16, F17) proceeds regardless.

**Post-implementation note (2026-09-12):** during Wave B's build, the
fix-implementer agent correctly stopped and reported that a migration WAS
needed after all — `document_embeddings` had no `DELETE` grant for `tc_app`,
required because the delete-then-reenqueue design unblocks the already-
accepted `EmbeddingModelConflictError` guard. The owner approved adding it
(migration `0009_document_embeddings_delete_grant.py`); see the ADR-0010
addendum for the full reasoning. Re-review also surfaced and fixed a real
bug after the first Wave B fix round: `has_dead_lettered_sync_events` was
unscoped by time, so a stale, unrelated dead-lettered event could
permanently fail every future reembed attempt for a workspace; a second,
independent bug (Postgres `now()` being frozen at transaction start, not
per-statement) was found and fixed in the *fix* for the first bug, before
either was committed — both are covered by tests exercising the real
`start()` → `has_dead_lettered_sync_events` path against genuinely
DB-inserted rows, not hand-supplied timestamps. Two residual, accepted
risks (a low-probability concurrent-start race; a document-deleted-mid-
sweep false-failure edge case) are recorded in the ADR-0010 addendum rather
than fixed in this pass.
