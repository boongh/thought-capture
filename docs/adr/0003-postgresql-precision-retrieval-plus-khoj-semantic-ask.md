# ADR-0003: PostgreSQL precision retrieval plus self-hosted Khoj for semantic/Ask

- **Status:** Accepted
- **Date:** 2026-09-02
- **Design anchor:** `docs/DESIGN.md` 7-9, 12.1, 12.2, 18 (Phase 2), 19, 20
- **First implemented in:** `packages/infrastructure/src/tc_infrastructure/db/search_reader.py` (exact path, slice 16), `packages/infrastructure/src/tc_infrastructure/khoj/` (Khoj adapter, slice 17)

## Context

`docs/DESIGN.md` already accepts the retrieval split this ADR formalizes:
PostgreSQL owns canonical facts, provenance, revisions, deterministic
filters, and lexical search; self-hosted Khoj owns semantic indexing and
Ask/RAG over the exported Markdown of generated documents. Forking Khoj is
explicitly out of scope (AGPL source-distribution obligations); integration
is API-only, against a pinned image. What this ADR adds is the concrete
version, deployment topology, and auth-mode decisions the design left open,
plus the results of the "Khoj contract spike" `docs/DESIGN.md` 7.6 and 19
defer to first integration.

**Version.** Every published Khoj release tag is `2.0.0-beta.N`; there is no
GA (`2.0.0`) tag as of this writing. The newest, `2.0.0-beta.28`, was released
2026-03-26 - about five months before this ADR, with no newer tag in between
(checked via the GitHub Releases API 2026-09-02). The owner accepted pinning
to this beta tag rather than waiting for a GA release that may not come on a
useful timeline; this is a deliberate, named risk, not an oversight.

**Image and digest.** The official image is `ghcr.io/khoj-ai/khoj`, not a
Docker Hub image. A digest scraped from GHCR's own (client-rendered) package
page could not be verified and was discarded rather than guessed. The real
digest was resolved the only trustworthy way - `docker pull` then `docker
inspect --format='{{index .RepoDigests 0}}'` - against the actual pulled
image:

```
ghcr.io/khoj-ai/khoj:2.0.0-beta.28
  @sha256:eb2e44669df44b51cb206b394dc0a00c782ac152dda02c97c9e3dac3d643dbb4
```

**Python compatibility.** Khoj's `pyproject.toml` declares `>=3.10, <3.13`,
confirmed unchanged from `docs/DESIGN.md`'s existing statement. It is run as
a separate container image, never sharing the first-party Python 3.14
environment (`docs/DESIGN.md` 1, 19).

**Deployment topology: Khoj gets its own PostgreSQL, not a shared server.**
`docs/DESIGN.md` 13/153 permits sharing one PostgreSQL *server* (separate
databases and roles) locally, splitting only for a VPS. Khoj's own reference
deployment provisions a dedicated `pgvector/pgvector:pg15` container as its
backing store - a different base image than the project's own
`postgres:18.6-trixie` (`deploy/compose/docker-compose.yml`). Sharing one
server would mean standardizing the shared server's base image on a
pgvector-capable build purely to satisfy Khoj's internal schema requirement,
coupling the first-party database's image choice to an implementation detail
of a system explicitly declared replaceable (`docs/DESIGN.md` 5 invariant 7:
"Provider isolation"). The owner chose a separate `khoj-db` container instead:
one more container locally, zero coupling, and it matches Khoj's own tested
configuration rather than an unverified alternative.

**Auth mode: anonymous, loopback-bound.** Khoj has its own per-user auth
concept - Django users, magic-link/OAuth login, per-user API tokens and chat
history - entirely separate from this project's workspace concept
(`docs/DESIGN.md` 6). Confirmed via `docs.khoj.dev/advanced/authentication`
and Khoj's own reference compose (`command: ... --anonymous-mode
--non-interactive`): self-hosted Khoj defaults to *not* anonymous unless the
flag is passed. `--anonymous-mode` grants every request Khoj's built-in
anonymous default user, so this project's own gateway remains the only real
authorization boundary (`docs/DESIGN.md` 10) - the Khoj port is never exposed
beyond `127.0.0.1` (`docs/DESIGN.md` 12.2), matching the existing pattern for
PostgreSQL and the debug UI (`docs/adr/0009`). The owner's decision: Khoj's
own multi-user machinery is deliberately never used as an isolation boundary,
and a second Khoj "user" must never be created - doing so would silently
introduce a second, unrelated partitioning scheme alongside `workspace_id`.

## Contract spike findings

`docs/DESIGN.md` 19's "Strict-filter Ask evidence injection: resolve during
Khoj contract spike" and 7.6's first-integration-milestone question were
tested directly: `ghcr.io/khoj-ai/khoj:2.0.0-beta.28` was pulled, run against
a throwaway `pgvector/pgvector:pg15` database with `--anonymous-mode`, and
exercised over its real HTTP API (`/openapi.json` fetched from the running
container for authoritative request/response shapes, not just source
reading). Findings:

1. **The documented API surface exists and matches.** `PUT /api/content`
   (multipart, `files` field, `t=markdown`), `DELETE /api/content/file`
   (`?filename=`) and `DELETE /api/content/files` (bulk, JSON
   `{"files": [...]}`), and `GET /api/search` (`q`, `n`, `t`, `r`,
   `max_distance`, `dedupe`) all work unauthenticated under anonymous mode,
   confirming `docs/DESIGN.md` 8/9's assumptions. No Khoj API token is needed
   for this project's use.

2. **A real search response, captured live:**

   ```json
   [{
     "entry": "<the full uploaded file content, front matter included>",
     "score": 0.1489...,
     "cross-score": null,
     "additional": {
       "source": "computer",
       "file": "project/test-doc--abc123.md",
       "uri": "file://project/test-doc--abc123.md#line=1",
       "compiled": "...",
       "heading": "..."
     },
     "corpus-id": "<khoj-internal uuid>"
   }]
   ```

   `additional.file` echoes back the exact filename uploaded - confirming
   `docs/DESIGN.md` 8.3's filename convention
   (`{workspace_id}/{kind}/{stable_key_slug}--{document_id}.md`) round-trips
   through Khoj intact, so the adapter can recover `workspace_id`/`document_id`
   from `additional.file` without depending on `entry`'s YAML front matter
   being parsed correctly by Khoj itself.

3. **Khoj caches identical search queries.** Deleting an indexed file and
   immediately repeating the *exact same* query string returned the deleted
   document from `khoj.routers.helpers: Return response from query cache`,
   even though `GET /api/content/files` correctly showed the file gone and a
   *differently worded* query correctly returned no results. This is a
   query-response cache, not a stale index - but it means a user re-asking
   the identical question shortly after new content is indexed can receive a
   cached, pre-update answer. Not addressed by this ADR (indexing/search only,
   no Ask yet); named here so slice 20 (Ask proxy) does not rediscover it.
   `tests/contract/khoj` uses distinct query text per test specifically to
   avoid this cache making a test pass for the wrong reason.

4. **`POST /api/chat` requires Khoj's own configured chat model.** With no
   `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`OPENAI_BASE_URL` set, Khoj logs "No
   default conversation config found, skipping default agent creation" at
   startup, and a chat call would need one configured. This means **Ask
   requires giving Khoj its own LLM credentials, separate from this project's
   own OpenRouter adapter and `docs/adr/0006`'s safe/custom mode controls** -
   raw retrieved document content and the user's question would go directly
   to whatever model Khoj is configured with, outside this project's own
   provider-routing/retention enforcement entirely. This is a real,
   consequential, unresolved privacy question - explicitly **not** decided by
   this ADR. It belongs to slice 20 (Ask proxy) and needs an owner decision
   (e.g., point Khoj's `OPENAI_BASE_URL` at OpenRouter using the same
   safe-mode reviewed-model/ZDR controls `docs/adr/0006` already established,
   rather than a separate, unreviewed credential) before any Ask code ships.

5. **Startup cost is acceptable for local development.** From container start
   to "Uvicorn running", ~30 seconds with the image already pulled (loading
   local embedding model `thenlper/gte-small` and reranker
   `mixedbread-ai/mxbai-rerank-xsmall-v1`, both CPU, both local - no
   embeddings sent externally, matching `docs/DESIGN.md` 9.2's stated
   preference). Image size ~5GB.

6. **Telemetry.** `KHOJ_TELEMETRY_DISABLE=True` is honored (confirmed via the
   startup log line "📡 Telemetry disabled") and is set unconditionally in
   this project's deployment - self-hosting Khoj specifically to keep personal
   data local is undermined by leaving telemetry on by default.

## Decision

1. Pin `ghcr.io/khoj-ai/khoj:2.0.0-beta.28` by digest (above) in
   `deploy/compose/docker-compose.yml`, under the existing `ai` profile.
2. Khoj gets its own PostgreSQL (`pgvector/pgvector:pg15`), a separate
   container from the first-party `postgres` service - not a shared server.
3. Run with `--anonymous-mode --non-interactive`, bound to `127.0.0.1` only.
   No Khoj API token is provisioned or used.
4. `KHOJ_TELEMETRY_DISABLE=True` always.
5. `sandbox` (Terrarium code execution), `search` (SearXNG web search), and
   `computer` (browser operator) - three services in Khoj's own reference
   compose - are not included. `docs/DESIGN.md` scopes Release 1 to search
   and Ask over this project's own captured/generated content; code
   execution, live web search, and computer-use are out of scope and would
   each need their own privacy review before being added.
6. Khoj's own chat-model configuration (needed for `POST /api/chat`/Ask) is
   explicitly deferred to slice 20, not decided here (see finding 4 above).

## Consequences

**Positive.** The image, digest, topology, and auth mode are now concrete and
verified against a real running instance, not inferred from documentation
alone - `docs/DESIGN.md` 19's contract-spike prerequisite is satisfied for the
indexing/search surface.

**Positive.** Loopback-only, anonymous-mode, zero-Khoj-credential deployment
means there is no Khoj-side secret to provision, rotate, or leak for the
indexing/search path - this project's own bearer token remains the only
credential a client needs.

**Negative.** Beta-track pinning (finding: no GA release exists) means an
upgrade may need to track breaking changes in a pre-1.0-numbered project more
often than a GA dependency would. Mitigated by `docs/DESIGN.md` 8.4's existing
pinned-digest-plus-contract-test upgrade policy - an upgrade is a deliberate,
tested branch, never a floating `latest`.

**Negative.** A separate `khoj-db` container is one more moving part locally
than sharing a server would have been. Accepted in exchange for zero coupling
between the first-party PostgreSQL image and Khoj's internal pgvector
requirement.

**Deferred.** Khoj's own chat-model/provider configuration for Ask (finding
4) - a privacy-relevant decision requiring explicit owner sign-off, not an
implementation detail. `docs/DESIGN.md` 7.6's "strict filters return a clear
capability code" fallback remains in force until this is resolved.

**Deferred.** The query-response cache (finding 3) is not mitigated in this
ADR; slice 19/20 should decide whether a cache-busting parameter, a documented
staleness window, or accepting it as-is is appropriate once Ask is built.

## Verification

Manual: `docker pull`/`docker inspect` for the digest above; a running
`ghcr.io/khoj-ai/khoj:2.0.0-beta.28` container against a throwaway
`pgvector/pgvector:pg15` database, exercised via `curl` against
`/api/content` (PUT/DELETE), `/api/content/files`, and `/api/search`, and its
live `/openapi.json` fetched for schema confirmation. Containers and volumes
were torn down afterward; nothing from this spike is deployed.

Automated: `tests/contract/khoj` (slice 17) pins the same digest and asserts
the behaviors named in findings 1-2 above against a real container started
for the test run.

## Review remediation (2026-09-02)

Independent review of PR #17 held merge pending four fixes, all applied on
this same branch per `docs/git-branching-policy.md` (findings on the
introducing, not-yet-merged branch land there, not deferred):

1. **CI never started Khoj.** `.github/workflows/ci.yml` had no Khoj step at
   all, so `docs/DESIGN.md` 16's required "Khoj API contract smoke test
   against the pinned digest" was not actually running anywhere - only
   `scripts/check.ps1`/`check.sh`'s local, developer-convenience skip-if-
   unreachable path existed, and CI never made that instance reachable. CI now
   starts `khoj-db`/`khoj-init`/`khoj` via the real `ai` compose profile,
   polls `/api/search` for readiness, then runs `pytest -m contract` with
   `TC_REQUIRE_CONTRACT=1` (a skip is a hard failure, matching how
   `TC_REQUIRE_INTEGRATION` already treats PostgreSQL), and tears the profile
   down unconditionally afterward. The local scripts' skip-if-unreachable
   behavior is intentionally unchanged - it is a developer convenience, not
   the design's CI requirement, and is documented as such in both scripts.

2. **Khoj ran as root with writable mounts under `/root`,** contrary to
   `docs/DESIGN.md` 13 ("non-root containers"). The pinned image ships no
   built-in non-root user and no config-directory override - confirmed
   directly by inspecting the image (`khoj/utils/constants.py` hardcodes
   `~/.khoj/...`) - so a new one-shot `khoj-init` service (reusing the same
   pinned image purely for coreutils, run once as its own default root only
   to `chown` a fresh volume) prepares a `khoj-home` volume for a fixed
   non-root UID/GID (`1000:1000`), and the `khoj` service now runs as that
   UID with `HOME=/khoj-home`. Verified live against the pinned container,
   not assumed: index/search/delete all round-trip correctly running non-root
   (same behavior as the original root-mode spike). One additional bug
   surfaced only by this change and confirmed against the image's own source:
   `khoj/utils/cli.py`'s `--log-file` default (`~/.khoj/khoj.log`) is typed as
   a bare `pathlib.Path` and is never expanded, so root's accidental
   ability to write anywhere (including a stray literal `~` directory) had
   been masking it; `--log-file=/khoj-home/.khoj/khoj.log` is now passed
   explicitly, absolute, working around Khoj's own unexpanded default rather
   than depending on it.

3. **The `khoj-db` image (`pgvector/pgvector:pg15`) was pinned by tag only,**
   unlike every other image in this compose file. Resolved by the same
   `docker pull` + `docker inspect --format='{{index .RepoDigests 0}}'`
   method used for the `khoj` image above:
   `sha256:a947c45cdc5906a1bc951f20a8709e321256343ee0f251e4ae00b5e7def4e6da`.

4. **A malformed Khoj search response (missing `entry`/`score`, a non-numeric
   score, a non-list body) surfaced as a bare `KeyError`/`TypeError`/
   `ValueError` from `HttpKhojClient.search`,** not `KhojUnavailableError` -
   a caller that only catches the latter (as `docs/DESIGN.md` 7.5's degrade-
   explicitly contract requires) would miss it. `HttpKhojClient.search` now
   catches those three exception types around result construction and
   re-raises as `KhojUnavailableError`, covered by
   `tests/unit/test_khoj_client_errors.py`.

None of these change the Decision or Consequences sections above; they
correct the implementation to actually match them.

## Review remediation, round 2 (2026-09-02)

A second independent review of PR #17 (against the round-1 fixes above)
confirmed all four of those and raised one new **P1, blocking merge**:

5. **A core-only deployment could not even parse its own Compose stack.**
   `khoj-db`, `khoj-init`, and `khoj` used Compose's required-variable form
   (`${TC_KHOJ_DB_PASSWORD:?...}` etc.) while living in the *same* file as
   the `core` profile's services. Compose interpolates every service in
   every file passed to it - required-variable references included - before
   `--profile` filtering ever decides which services actually start
   (confirmed by reproduction: `docker compose -f docker-compose.yml
   --profile core config --quiet` with no `TC_KHOJ_*` set failed on a
   *Khoj* variable). This silently made the supposedly opt-in `ai` profile
   a hard dependency of `core`, contrary to this ADR's own "one more
   container locally, zero coupling" framing and to the PR's stated
   topology.

   Fixed by moving `khoj-db`/`khoj-init`/`khoj` and the `khoj-db-data`/
   `khoj-home` volumes into a new, separate file,
   `deploy/compose/khoj.docker-compose.yml`, loaded only with an explicit
   second `-f` when the `ai` profile is actually wanted. `core` now parses
   with zero `TC_KHOJ_*` configuration; `ai` still fails clearly (naming the
   specific missing variable) when its own secrets are absent. A regression
   check proving both halves - `docker compose --profile core config`
   succeeds with no Khoj variables set, and the two-file `--profile ai
   config` fails clearly without them - now runs in `.github/workflows/ci.yml`
   and both `scripts/check.ps1`/`check.sh` (gated on Docker being installed,
   matching the other Docker-dependent stages).

   Every doc/script/test that referenced the old single-file `--profile ai`
   invocation (`env.example`, `tests/contract/khoj/conftest.py`, the CI
   workflow, both check scripts) was updated to the two-file form.

## Index sync run-tracking scope (2026-09-03, slice 18)

`docs/DESIGN.md` 7.2 step 11 reads: "Index changed files in Khoj and record
item hashes. Failure marks the run `partial`; canonical revisions remain
valid and sync retries independently." Read most literally, this implies
Khoj indexing happens synchronously inside the organize run itself, with a
failure flipping that run's own `runs.status` to `partial` - a status value,
and the `runs.kind = 'khoj_sync'` enum member (`docs/DESIGN.md` 6.3), that
existed in the schema from `docs/adr/0004` onward but had no code path ever
setting either one.

**Decision, put to the owner directly during slice 18's implementation
planning and confirmed:** index sync is fully decoupled from the organize
run and from `runs` entirely - the same outbox-consumer shape already
established for digest delivery (`tc_application.digest_delivery.DeliverDigests`
/ `tc_infrastructure.db.digest_outbox.PostgresDigestOutbox` /
`tc_discord_bot.digest_loop.DigestDeliveryLoop`, none of which touch `runs`
either). `PostgresOrganizeWriter.write` enqueues one `khoj.sync_requested`
outbox event per document written, in the same transaction as the revision;
a worker-side poller (`tc_worker.khoj_sync_loop.KhojSyncLoop`, identical
shape to the bot's digest loop) drives `tc_application.khoj_sync.DeliverKhojSync`
on a fixed interval. The organize run itself is marked `succeeded` purely on
the LLM-and-write step completing, exactly as it already was before this
slice - a downstream Khoj sync failure never touches it. Per-document sync
health is observable through `khoj_index_items` (last *successful* sync
only: filename, revision, sha256, timestamp - see migration `0007`'s own
docstring for why a failed attempt is deliberately not recorded there) and
through the `khoj.sync_requested` outbox event's own `attempts`/`last_error`
for anything still failing.

**Why not the literal reading.** Two considered alternatives were rejected:

1. Flip the *organize* run's own `status` to `partial` on a Khoj failure.
   Rejected because Khoj indexing is asynchronous by design (docs/DESIGN.md
   14.1: "Khoj unavailable: canonical documents commit; sync retries; exact
   search remains available") - the organize run has typically already
   returned success to its caller (`/organize`, the scheduler) well before a
   sync event is even claimed, so retroactively downgrading a already-reported
   `succeeded` run to `partial` minutes or hours later has no consumer that
   would ever see it happen, and would make `runs.status` mean two different
   things depending on how much time has passed since it was read.
2. A dedicated `runs` row per sync attempt (`kind='khoj_sync'`), giving
   `GET /v1/runs/{id}`-style observability for sync specifically. Rejected as
   unnecessary machinery for what this slice actually needs: `docs/DESIGN.md`
   14.2 lists "Khoj sync lag" as an observability target, which
   `khoj_index_items.synced_at` compared against `document_revisions.created_at`
   already answers without a `RunLedger` integration, additional tests, and a
   second status-transition surface to keep consistent with the outbox's own.

**Consequence.** `runs.kind = 'khoj_sync'` and `runs.status = 'partial'`
remain reserved-but-unused after this slice, matching their state before it.
If a future need for run-level sync observability emerges (e.g. an operator
wants "list every sync attempt for window X" the way `/v1/runs/{id}` already
does for organize), that is new work requiring its own decision, not an
oversight in this one.

This section is the ADR update `docs/DESIGN.md` 16 and CLAUDE.md require
for a deviation from an already-accepted pipeline step's stated behavior;
`docs/DESIGN.md` 7.2 step 11's own text is intentionally left as written
(matching this document's own precedent of not silently rewriting
accepted design text) with this ADR as the authoritative gloss on what "the
run" refers to and how failure is actually tracked.

## Ask proxy: gated resolution of finding 4 (2026-09-07, slice 20)

Finding 4 above left Ask's privacy question explicitly undecided: `POST
/api/chat` needs Khoj's own configured chat model, so a question and its
retrieved note content would go to whatever model Khoj is configured with -
outside this project's own OpenRouter adapter and `docs/adr/0006`'s
safe/custom-mode controls entirely. Slices 18/19 (index sync, hybrid search)
both deliberately left this deferred; this section is slice 20's decision.

**Decision.** The same two-mode pattern `docs/adr/0006` already established
for organize/select applies here, not a new mechanism: an explicit,
greppable, default-off gate rather than a silent default in either
direction.

- **`TC_ASK_ENABLED`** (`Settings.ask_enabled`, default `false`) is this
  project's own gate. `tc_application.ask.AskQuestion` is constructed with
  `enabled=` resolved once at composition time (`apps/api/src/tc_api/app.py`,
  `apps/discord_bot/src/tc_discord_bot/__main__.py`) - the same
  build-time-resolved-value pattern `OrganizeWindow` already uses instead of
  reading `Settings` itself (`docs/DESIGN.md` 5.3). When `False`,
  `AskAnswer.enabled=False` and no call to Khoj is made at all.
- **Khoj's own chat-model configuration** (finding 4) is a second,
  independent gate this project's code never sets:
  `TC_KHOJ_OPENAI_BASE_URL`/`TC_KHOJ_OPENAI_API_KEY` (blank by default,
  `env.example`) pass straight through to the `khoj` container's
  `OPENAI_BASE_URL`/`OPENAI_API_KEY`, which Khoj's own `--non-interactive`
  first-boot setup (`khoj.utils.initialization._create_chat_configuration`,
  verified from source at the pinned tag `2.0.0-beta.28`) reads to
  auto-register a chat model. This can point at a local server (Ollama,
  vLLM, LM Studio) or at OpenRouter using `docs/adr/0006`'s own safe-mode
  reviewed-model/ZDR discipline - the choice, the credential, and the model
  all remain entirely the operator's.

Both must be true before any question reaches a live model. Leaving either
at its default keeps `/v1/ask` and Discord `/ask` reporting `enabled: false`
rather than guessing, and `KhojUnavailableError` (Khoj unreachable, *or* -
per finding 4 - Khoj reachable but with no chat model configured, since this
project cannot distinguish those from the outside without guessing at
Khoj's own error shape) maps to `degraded: true`, never a fabricated answer.

**`/api/chat`'s request/response shape.** Not part of this ADR's original
contract spike (finding 4 only observed the *absence* of a configured
model). `ChatRequestBody` (request: `q`, `n`, `stream`, `create_new`, ...)
and the non-streaming branch of `POST /api/chat` (response:
`{"response", "references": {"context": [...]}, "usage", ...}`, where each
`context` item carries a `"file"` key in the same convention as
`/api/search`'s `additional.file`) were read directly from
`khoj-ai/khoj`'s source at the pinned tag - a different evidence standard
than this ADR's live-container spike for `/api/search`/`/api/content`, named
here so it is not mistaken for the same level of verification. The query is
prefixed `/notes ` to force Khoj's notes-only retrieval command and skip
online search/code execution (both undeployed anyway, decision 5 above).

**Live-verified separately:** with the pinned image running and no
`OPENAI_BASE_URL`/`OPENAI_API_KEY` set, `POST /api/chat` returns a plain
`500 Internal Server Error` with no JSON body - not a `200` with an
error-shaped payload. `HttpKhojClient.chat`'s `response.raise_for_status()`
already converts this to `KhojUnavailableError` correctly, with no special
case needed for the unconfigured path specifically.

**Reference resolution.** `AskQuestion` resolves each Khoj citation back to
a document through `SemanticHydrator` - the identical workspace-scoped,
filter-re-checked, current-revision-only path `Search._semantic_results`
already uses for search hits, not a second lookup mechanism. Filenames are
deduped before hydrating and again while building the reference list: Khoj
can chunk one uploaded document into more than one indexed entry sharing the
same filename and cite more than one chunk in the same answer, which would
otherwise return the same `AskReference` twice - the identical shape as a
known, separate issue in `Search._semantic_results`'s own consumption of
Khoj hits (see "Known gap" below).

**Known limitation, shared with the merged search code, not new here.**
`AskAnswer.answer` itself - Khoj's free-text prose - carries no workspace
check of its own; only the structured `references` list is re-resolved and
workspace-filtered through `SemanticHydrator`. `AskQuestion.__call__` calls
`KhojPort.chat` with no `workspace_id` parameter at all, the same shape
`Search._semantic_results`'s existing `khoj.search()` call already has.
Search is safe because a raw Khoj `entry` is discarded entirely and only
hydrator-resolved `SearchResult`s reach the caller; Ask's answer text has no
equivalent discard step, since the prose itself *is* the deliverable. Under
`docs/DESIGN.md`'s single-workspace-per-deployment model (section 6, "single-
user first, workspace-scoped always" - one Khoj instance holds exactly one
workspace's content today) this is unreachable, not a live gap. It becomes
one if a future deployment ever shares one Khoj index across workspaces -
tracked as a prerequisite for that change, not a defect in this one.

**Discord.** `/search` and `/ask` are registered the same way
`/organize`/`/status` already are (`_reject_if_not_owner`,
`_OWNER_ONLY_CONTEXTS` allowing DM and guild) - arguably a stronger case
than administrative commands, since these read or proxy the owner's own
memory content rather than only trigger a job. `/ask` reports
`enabled`/`degraded` in plain language rather than Discord rendering a raw
problem document.

**Known gap, not part of this slice's fix.** `tc_application.search.Search
._semantic_results` (slice 19) passes Khoj's raw hit list to
`fuse_rrf`/`SearchPage.items` without deduping by filename first. Because a
single uploaded document can legitimately produce more than one Khoj hit
(chunked ingestion), this can duplicate a result in `mode=semantic` and
inflate its fused score in `mode=hybrid`. `AskQuestion` (this slice) dedupes
its own consumption of Khoj hits to avoid the identical defect, but does not
fix `Search`'s - that is flagged separately for its own review rather than
folded into this change, since `Search`/`fuse_rrf` are already-merged,
already-reviewed code this slice does not otherwise touch.

**Verification.** `tests/unit/test_ask.py`: `enabled=False` makes zero calls
to `KhojPort`; `KhojUnavailableError` maps to `degraded=True`; references
are resolved via `SemanticHydrator` and deduped by filename.
`tests/unit/test_khoj_client_chat.py`: `HttpKhojClient.chat` against a
mocked `httpx` transport - the exact request body sent, correct parsing of
the verified response shape, and a non-string `"response"` raising
`KhojUnavailableError`. `tests/integration/test_api_ask.py`: `/v1/ask`
through the real ASGI app against real PostgreSQL, including a real
(unreachable-in-CI) `HttpKhojClient` so the degrade path is genuine. All of
`ruff format --check`/`ruff check`/`mypy`/`pytest -m unit`/`pytest -m
integration`/`pytest -m contract` pass - see the pull request for exact
counts. Independent review (`.claude/agents/code-reviewer.md`) ran against
this slice's diff before merge.

## Ask proxy: PR review remediation (2026-09-07)

Independent PR review of slice 20 (above) found five P1s, two P2s, and a P3
against the design this project actually accepted, not against a different
design - each is addressed here rather than by amending history above, per
this repository's convention of appending dated sections.

**P1: buffered, not streamed.** `docs/DESIGN.md` 7.6 ("the gateway streams
the answer") was not implemented - the first cut sent `stream: false` to
Khoj and buffered up to 60 seconds before returning a single JSON body.
Fixed by making streaming the shape everywhere: `KhojPort.chat` is now
`def chat(...) -> AsyncIterator[KhojChatChunk]` (`packages/domain/src/
tc_domain/khoj_ports.py`), `HttpKhojClient.chat` sends `stream: true` and
parses Khoj's `END_EVENT`-delimited wire protocol incrementally
(`packages/infrastructure/src/tc_infrastructure/khoj/client.py`),
`AskQuestion.__call__` yields `AskChunk`s instead of returning one
`AskAnswer` (`packages/application/src/tc_application/ask.py`), and
`POST /v1/ask` returns `StreamingResponse(..., media_type=
"application/x-ndjson")` - one `AskEvent` JSON object per line
(`apps/api/src/tc_api/routers/ask.py`, `apps/api/src/tc_api/schemas.py`).
Discord `/ask`, which needs one complete answer rather than a live typing
effect, consumes the same stream through a new `collect_ask_answer`
adapter (`packages/application/src/tc_application/ask.py`) rather than the
API and the bot diverging on `KhojPort`'s shape.

**P1: no strict-filter capability fallback.** `docs/DESIGN.md` 551 and 637
require a clear "filters unsupported" signal plus an exact-search
substitute when a caller asks for anything Khoj's semantic query cannot
express - the first cut accepted only `q` and silently ignored every other
field, which is not the same thing as declaring them unsupported. Fixed by
`has_strict_filters(query: SearchQuery) -> bool` (`packages/domain/src/
tc_domain/search.py`, checks every `SearchQuery` field except `q`) gating
`AskQuestion.__call__`: a structured filter now short-circuits before any
Khoj call, running `ExactSearchPort` instead and yielding a single
`AskChunk(strict_unsupported=True, fallback=<SearchPage>)`. `AskRequest`
gained the same filter fields `SearchRequest` already has
(`apps/api/src/tc_api/schemas.py`) so the API can actually receive them.

**P1: no pinned-Khoj contract test for the chat interface.** The original
slice's `/api/chat` request/response shape (previous section, "Not part of
this ADR's original contract spike") was read from `khoj-ai/khoj` source at
the pinned tag, never exercised against a real running instance - a weaker
evidence standard than every other adapter in this ADR, and explicitly
flagged as such at the time. Closed by building
`tests/contract/khoj/stub_chat_model.py`, a minimal OpenAI-compatible
server (`GET /v1/models`, `POST /v1/chat/completions`, both streaming and
non-streaming), and configuring a real pinned `2.0.0-beta.28` container
against it at first boot (`TC_KHOJ_OPENAI_BASE_URL`/`TC_KHOJ_OPENAI_API_KEY`
pointed at the stub before Khoj's own `--non-interactive` chat-model
registration ran). Live output against that setup:

```
{"type": "metadata", "data": {"conversationId": "...", "turnId": "..."}}
{"type": "status", "data": "**Searching Documents for:** ..."}
{"type": "status", "data": "**Found 1 Notes Across 1 Files**: ..."}
{"type": "references", "data": {"inferredQueries": [...], "context": [{"query": "...", "compiled": "...", "file": "test-note.md", "uri": "..."}], "onlineContext": {}, "codeContext": {}}}
{"type": "status", "data": "**Generating a well-informed response**"}
{"type": "start_llm_response", "data": ""}
This is a stub answer                          <- raw text, not JSON: a MESSAGE chunk
 for contract testing.
{"type": "end_llm_response", "data": ""}
{"type": "usage", "data": {...}}
{"type": "end_response", "data": ""}
```

This confirms every assumption `_parse_chat_event`/`_parse_chat_references`
made from source: `metadata`/`references`/raw-text `message` chunks are the
only three event shapes this adapter needs, `status`/`usage`/
`start_llm_response`/`end_llm_response`/`end_response` are correctly
ignored, and a `references` context item carries `compiled`/`file` but
*not* `heading` (handled by the existing `item.get("heading") or ""`
fallback - no crash, no test gap). It also surfaced one behavior neither
source-reading nor the original design anticipated: with nothing indexed
at all, Khoj answers with a canned "you haven't synced any notes yet"
message and never calls the configured chat model - a guardrail internal
to Khoj's own `/notes` command, not something this adapter needs to special
-case, since the response still arrives as an ordinary raw-text `message`
chunk. Formalized as `tests/contract/khoj/test_khoj_chat.py`
(`test_chat_answers_and_references_the_indexed_note`,
`test_chat_leaves_no_conversation_behind`), wired into CI
(`.github/workflows/ci.yml`, "Start the stub chat model" step, before "Start
Khoj") via `extra_hosts: ["host.docker.internal:host-gateway"]` added to the
`khoj` service (`deploy/compose/khoj.docker-compose.yml`) so a Linux CI
runner resolves the same hostname Docker Desktop already provides for free
locally.

**P1: no retention/purge policy for Khoj's own conversation copy.** Each
`/api/chat` call left a server-side Khoj conversation - a second,
unaccounted-for copy of personal-memory content this project's own backup/
export/retention story (`docs/DESIGN.md` 12.3, 14.3) does not cover. Fixed
by `HttpKhojClient.chat`'s `finally` block calling `_delete_conversation`
(`DELETE /api/chat/history?conversation_id=...`) immediately after each
call, using the `conversationId` the streaming `metadata` event surfaces -
the non-streaming response never exposed this id at all, so this fix was
only possible once streaming (above) landed. Best-effort: a cleanup failure
logs (`khoj_chat.conversation_cleanup_failed`) rather than masking the
answer the caller already received. Live-verified: `GET /api/chat/sessions`
before and after a real `HttpKhojClient.chat()` call against the
stub-configured instance above shows no new conversation id surviving the
call, while a conversation created by a raw `curl` call that bypasses this
adapter (and therefore never reaches the `finally` block) remains listed -
confirming the delete is this adapter's behavior, not an artifact of Khoj's
own conversation lifecycle. Covered by
`tests/contract/khoj/test_khoj_chat.py::test_chat_leaves_no_conversation_behind`.

**P1: the external chat-model route had no enforceable provider/retention
policy.** The original two-gate design (`TC_ASK_ENABLED` plus Khoj's own
`OPENAI_BASE_URL`/`OPENAI_API_KEY`) let an operator point Ask at an
unreviewed, non-ZDR provider with nothing but a doc comment standing in the
way - `docs/adr/0006`'s safe-mode reviewed-model/ZDR discipline is
enforced in code for organize/select but was only encouraged, not required,
here. Fixed by a third, independent gate:
**`TC_ASK_PROVIDER_RETENTION_ACKNOWLEDGED`** (`Settings
.ask_provider_retention_acknowledged`, default `false`,
`packages/infrastructure/src/tc_infrastructure/config.py`). A new
`@model_validator(mode="after") _ask_requires_provider_acknowledgment`
raises at startup if `TC_ASK_ENABLED=true` while this is `false` - the same
fail-fast pattern `_safe_mode_restricts_to_reviewed_models` already uses,
so a misconfigured deployment refuses to start rather than silently
exposing notes to an unreviewed model. This does not itself validate *which*
provider is configured (Khoj's `OPENAI_BASE_URL` remains this project's
adapter boundary, per this ADR's decision 5's "external services stay
behind a replaceable adapter"); it forces the operator to affirmatively
attest they have applied `docs/adr/0006`'s review requirements to whatever
they pointed `TC_KHOJ_OPENAI_BASE_URL` at, the same way enabling safe-mode
custom models already requires.

**P2: malformed `references` raised an uncaught `AttributeError`.** A
response whose `references.context` was not a list (or whose items were not
dicts) let Python's own `AttributeError`/`TypeError` escape
`HttpKhojClient.chat` as an unhandled `500`, rather than degrading like
every other malformed-Khoj-response case (`docs/DESIGN.md` 7.5). Fixed by
`_parse_chat_references` validating `isinstance(data, dict)` and
`isinstance(context, list)` explicitly before use, raising
`KhojUnavailableError` - the same exception type every other malformed-
shape case in this adapter already raises - instead of letting the
underlying Python exception surface. Covered by
`tests/unit/test_khoj_client_chat.py
::test_chat_raises_khoj_unavailable_on_a_malformed_references_event`,
parametrized over four malformed shapes (non-dict data, non-list context,
non-dict item, item missing `compiled`).

**P2: Discord `/ask` had no length bound, rate limit, concurrency limit, or
cost guard.** `/v1/ask` already rejected a question over 2,000 characters
(`AskRequest`'s Pydantic constraint); Discord `/ask` had no equivalent, and
nothing stopped a user from firing overlapping or rapid-fire questions at a
model that can carry real per-call cost once a provider is configured
(P1 above). Fixed in `apps/discord_bot/src/tc_discord_bot/commands.py`:
`_ask_question_too_long` mirrors the API's 2,000-character bound exactly
(`_ASK_QUESTION_LIMIT`); a mutable `_ask_in_flight` cell rejects a second
`/ask` while one is already running (single-owner bot, so a simple flag is
sufficient - no cross-process coordination needed); `_ask_cooldown_remaining_seconds`
enforces a 15-second cooldown between calls (`_ASK_COOLDOWN_SECONDS`).
Deliberately *not* `app_commands.checks.cooldown`: this bot's
`CommandTree` has no `on_error` override, so a failed check would raise
`AppCommandError` and leave the interaction unanswered ("This interaction
failed" in Discord) instead of the explicit, testable message these
hand-written checks send via `interaction.response.send_message`, matching
the existing `_reject_if_not_owner` pattern rather than introducing a
second error-handling convention. Covered by `tests/unit/
test_discord_commands.py`'s length/cooldown/concurrency tests.

**P3: `docs/OPERATING.md` described the wrong degrade behavior.** It
claimed an unconfigured Khoj chat model makes `/ask`/`/v1/ask` report
`enabled: false`; the actual (and correct, per this ADR's original
decision) behavior is `enabled: true, degraded: true` - `enabled` reflects
only this project's own `TC_ASK_ENABLED` switch, since the code has no way
to know Khoj's chat model is unconfigured until a call to it actually
fails. Fixed in `docs/OPERATING.md`'s Ask section, which now also documents
the third `TC_ASK_PROVIDER_RETENTION_ACKNOWLEDGED` gate above.

**Verification.** `tests/unit/test_ask.py` (rewritten for streaming:
disabled short-circuit, streamed collection, mid-stream partial-answer
degrade, strict-filter fallback parametrized over every filter field),
`tests/unit/test_khoj_client_chat.py` (rewritten against `httpx
.MockTransport` for the streaming wire format, including the malformed-
references parametrized case), `tests/unit/test_discord_commands.py`
(length/cooldown/concurrency), `tests/integration/test_api_ask.py`
(rewritten for NDJSON, including the strict-filter-fallback path against
real PostgreSQL), `tests/contract/khoj/test_khoj_chat.py` (new, live
against a real stub-configured pinned Khoj container). All of `ruff format
--check`/`ruff check`/`mypy`/`pytest -m unit`/`pytest -m integration`/
`pytest -m contract` pass - see the pull request for exact counts.
Independent review ran against this remediation's diff before merge.
