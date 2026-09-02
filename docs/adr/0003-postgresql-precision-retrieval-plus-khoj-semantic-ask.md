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
