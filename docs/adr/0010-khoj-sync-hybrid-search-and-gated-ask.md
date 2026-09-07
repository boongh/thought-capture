# ADR-0010: Khoj index sync, hybrid search, and an explicitly-gated Ask proxy

- **Status:** Accepted
- **Date:** 2026-09-07
- **Design anchor:** `docs/DESIGN.md` 7.5, 7.6, 8, 10, 19; extends `docs/adr/0003`
- **First implemented in:** `packages/domain/src/tc_domain/khoj_export.py`, `packages/domain/src/tc_domain/ask.py`, `packages/application/src/tc_application/{search,khoj_sync,ask}.py`, `packages/infrastructure/src/tc_infrastructure/khoj/client.py`, `apps/api/src/tc_api/routers/{search,ask,admin}.py`, `apps/discord_bot/src/tc_discord_bot/commands.py`

## Context

Exact search shipped (slice 16) but `mode=semantic`/`mode=hybrid` returned a
hard `501` (`tc_application.search.Search`'s `SUPPORTED_MODES = ("exact",)`).
The Khoj adapter shipped (slice 17, `docs/adr/0003`) but nothing ever called
`KhojPort.index` - Khoj's own index has always been empty, so even a wired
semantic channel would have nothing to return. `docs/adr/0003`'s contract
spike also left Ask entirely undecided: finding 4 established that
`POST /api/chat` requires Khoj's own configured chat model, which means
question text and retrieved note content would go to whatever model Khoj is
configured with, **outside this project's own OpenRouter adapter and
`docs/adr/0006`'s safe/custom-mode controls entirely** - "a real,
consequential, unresolved privacy question - explicitly not decided by this
ADR. It belongs to slice 20 (Ask proxy) and needs an owner decision... before
any Ask code ships." `tc_discord_bot/commands.py`'s own docstring recorded the
same blocker: "`/ask` is blocked on the Khoj-chat-credential privacy decision
ADR-0003 explicitly defers to the owner."

The owner's direction for this slice (session 2026-09-07) was to finish
`/search` and `/ask`, including from Discord, with independent review and
tests. This ADR is how that direction resolves the deferred decision, rather
than the code silently making it.

**`/api/chat`'s exact shape was not part of `docs/adr/0003`'s live spike**
(finding 4 only observed the *absence* of a configured model, not a
successful call's response). Rather than guess, `ChatRequestBody` (request
shape: `q`, `n`, `stream`, `create_new`, ...) and the non-streaming branch of
`POST /api/chat` (response shape: `{"response", "references": {"context": [...]}, "usage", ...}`,
where each `context` item carries a `"file"` key in the same convention as
`/api/search`'s `additional.file`) were read directly from
`khoj-ai/khoj`'s source at the exact pinned tag, `2.0.0-beta.28`
(`src/khoj/routers/api_chat.py`, `src/khoj/utils/rawconfig.py`,
`src/khoj/routers/helpers.py`). This is a different evidence standard than
`docs/adr/0003`'s live-container spike for `/api/search`/`/api/content` - real
source at the exact deployed commit, not a live call with a configured model -
and is named here so it is not mistaken for the same level of verification.
A live contract test against a real configured chat model (mocked upstream,
so no cost or real credentials) is a documented follow-up, not built this
slice.

## Decision

### 1. Khoj index sync: full resync only, operator-triggered

`POST /v1/admin/khoj-sync` (`tc_application.khoj_sync.SyncKhojIndex`) lists
every current document revision in the workspace
(`PostgresDocumentReader.list_all_current_for_export`, unpaginated - a
personal corpus's document count is small, `docs/DESIGN.md` 7.3.1), renders
each as Markdown (`tc_domain.khoj_export.khoj_markdown`, `docs/DESIGN.md`
8.3's front-matter format, using `json.dumps` for YAML-safe scalar/sequence
quoting rather than a hand-rolled escaper), and calls `KhojPort.index` for
all of them. Khoj's own upload endpoint replaces content by filename
(`docs/adr/0003` finding 1), so re-uploading an unchanged document is a safe,
idempotent no-op.

**Deliberately not built this slice:**
- **Incremental sync.** No `khoj_index_items` tracking table
  (`docs/DESIGN.md` 6.5) yet - every sync re-renders and re-uploads every
  current document, not just changed ones. Correct, not efficient; a
  follow-up.
- **Automatic sync after organize** (`docs/DESIGN.md` 7.2 steps 10-11).
  Wiring this into `OrganizeWindow` - already the highest-risk, most-recently
  -stabilized pipeline in the codebase (`x-ai/grok-4.3` just went live) -
  was judged a larger, separately-reviewable change than this slice's actual
  ask. Today, Khoj only has content after an operator calls
  `POST /v1/admin/khoj-sync` explicitly; there is no Discord command for it
  either, matching the API-only precedent `docs/DESIGN.md` 10 already sets
  for `/v1/admin/export`.
- **The query-response cache** (`docs/adr/0003` finding 3). Accepted as-is
  for Release 1, per that finding's own framing.

### 2. Hybrid search: RRF on rank position, not raw Khoj score

`tc_application.search.Search` gains `semantic` and `hybrid` modes.
`semantic` calls `KhojPort.search`, maps each hit's filename back to a
`document_id` (`tc_domain.khoj_export.parse_khoj_filename`, verifying the
embedded `workspace_id` before trusting a hit - defense in depth; unreachable
under `docs/adr/0003`'s single-anonymous-instance deployment today, but must
never silently trust a filename as an isolation boundary if that changes),
and hydrates full result rows from PostgreSQL
(`PostgresExactSearch.hydrate`, a new `ExactSearchPort` method) - PostgreSQL
remains the source of truth for what is citable (`docs/DESIGN.md` 8.2), so a
Khoj hit whose document is no longer the current revision is dropped, not
surfaced with nothing behind it.

`hybrid` runs both channels and fuses with RRF, `k=60` (`docs/DESIGN.md`
7.5): `score = sum(1 / (60 + rank))` over every channel a result appears in,
using **each channel's own returned rank position**, not Khoj's raw
similarity score - `docs/adr/0003`'s contract spike did not establish
whether Khoj's score is "higher is better" or a distance metric, and RRF only
needs relative order within a channel, so this sidesteps that ambiguity
entirely rather than guessing a direction. Exact-phrase results (`query.phrase`
set) receive a fixed `+1.0` boost, guaranteeing they outrank any
non-phrase-matching hybrid result - phrase is a verified substring match,
strictly more certain than a similarity score. Both `semantic` and `hybrid`
degrade explicitly (`SearchPage.degraded=True`) on `KhojUnavailableError` or
when no Khoj adapter is wired at all, matching `docs/DESIGN.md` 7.5's
"never return an empty success that implies no memory exists".

**Deliberately not built this slice:** `semantic`/`hybrid` pagination.
`next_cursor` is always `None` for these two modes - RRF fusion needs the
whole result set from both channels to rank correctly, which does not
compose with a simple keyset offset the way exact search's single-channel
cursor does. A follow-up, not implemented here.

### 3. Ask: built, but gated behind two independent opt-ins

`tc_application.ask.AskQuestion` calls `KhojPort.chat` (`POST /api/chat`,
`docs/DESIGN.md` 7.6's `/notes`-only mode - the query is prefixed
`/notes ` to force Khoj's notes-only retrieval command and skip online
search/code execution, both of which are undeployed anyway,
`docs/adr/0003` decision 5) and maps returned references back to citable
documents the same way `_semantic` does.

This is where `docs/adr/0003` finding 4's deferred decision resolves, and it
resolves the same way `docs/adr/0006` already resolves an analogous
question for organize/select: **an explicit, greppable, default-off gate**,
not a silent default in either direction.

- **`TC_ASK_ENABLED`** (`Settings.ask_enabled`, default `false`) is this
  project's own gate. `AskQuestion` is constructed with `enabled=` resolved
  once at composition time (`apps/api/src/tc_api/app.py`,
  `apps/discord_bot/src/tc_discord_bot/__main__.py`) - the same
  build-time-resolved-value pattern `OrganizeWindow` already uses instead of
  reading `Settings` itself (`docs/DESIGN.md` 5.3). When `False`,
  `AskAnswer.enabled=False` and no call to Khoj is made at all.
- **Khoj's own chat-model configuration** (`docs/adr/0003` finding 4) is a
  second, independent gate this project's code never sets:
  `TC_KHOJ_OPENAI_BASE_URL`/`TC_KHOJ_OPENAI_API_KEY` (blank by default,
  `env.example`) pass straight through to the `khoj` container's
  `OPENAI_BASE_URL`/`OPENAI_API_KEY`, which Khoj's own
  `--non-interactive` first-boot setup
  (`khoj.utils.initialization._create_chat_configuration`, verified from
  source at the pinned tag) reads to auto-register a chat model. This can
  point at a local server (Ollama, vLLM, LM Studio) or at OpenRouter using
  `docs/adr/0006`'s own safe-mode reviewed-model/ZDR discipline - the choice,
  the credential, and the model all remain entirely the operator's.

Both must be true before any question reaches a live model. Leaving either
at its default keeps `/v1/ask` and `/ask` reporting `enabled: false` rather
than guessing, and `KhojUnavailableError` (Khoj unreachable, *or* - per
finding 4 - Khoj reachable but with no chat model configured, since this
project cannot distinguish those from the outside without guessing at
Khoj's own error shape) maps to `degraded: true`, never a fabricated answer.

**Verified against a live, actually-unconfigured instance** (update,
2026-09-07, after this ADR's first draft): with the pinned image running and
no `OPENAI_BASE_URL`/`OPENAI_API_KEY` set, `POST /api/chat` returns a plain
`500 Internal Server Error` with no JSON body at all - not a `200` with an
error-shaped payload, as the first draft of this ADR left open as a
possibility. `HttpKhojClient.chat`'s existing `response.raise_for_status()`
already converts this to `KhojUnavailableError` correctly, with no code
change needed; the non-string-`"response"`-field defense
(`MessageProcessor.handle_json_response`'s dict-shaped image/error payload)
remains as a second, independent layer for a *different* failure shape
(a configured-but-degenerate response), not the one actually observed here.

### 4. Discord: `/search` and `/ask`, owner-gated like `/organize`/`/status`

Both commands are registered the same way `/organize`/`/status` already are
(`_reject_if_not_owner`, `_OWNER_ONLY_CONTEXTS` allowing DM and guild) -
arguably a stronger case than administrative commands, since these read or
proxy the owner's own memory content rather than only trigger a job.
`/search` defaults to `mode=exact` (always available, no sync required);
`/ask` reports `enabled`/`degraded` in plain language rather than Discord
rendering a raw problem document. Replies use plain-text truncation at
Discord's 2000-character limit, not the digest's section-boundary splitting
(`docs/DESIGN.md` 4.2) - a deliberate scope cut for ephemeral diagnostic
replies, not an oversight.

## Consequences

**Positive.** `/v1/search` (all three modes) and `/v1/ask` are both real,
callable, and reachable from Discord, closing the two-command gap
`tc_discord_bot/commands.py` had documented since slice 17.

**Positive.** Ask's privacy question is resolved the way this codebase
already resolves this shape of question (`docs/adr/0006`'s safe/custom split)
rather than left open or decided silently: two independent, operator-owned
opt-ins, neither of which this project's own code can flip on its own behalf.

**Negative.** Semantic/hybrid search is only as fresh as the last manual
`POST /v1/admin/khoj-sync` - a newly-organized document will not appear in
semantic results until an operator (or a future automation) re-syncs. Exact
search is unaffected (it always reads PostgreSQL's current state directly).

**Negative.** `/api/chat`'s *successful-response* shape is still verified
from source only, not from a live call with a real configured model -
weaker evidence than `docs/adr/0003`'s `/api/search`/`/api/content` spike.
The *unconfigured* failure path (the likeliest early-adopter path, since
Ask defaults off on both sides) is now live-verified (see above). If Khoj's
actual successful-response behavior diverges from its own source at the
pinned tag, `HttpKhojClient.chat`'s parsing could break in a way unit tests
against a synthetic response would not catch. Mitigated by defensive
shape-checking (any missing/malformed field raises `KhojUnavailableError`
rather than crashing), but not eliminated - a live contract test against a
real configured (stub) chat model remains a follow-up.

**Negative → fixed before merge.** Independent review
(`.claude/agents/code-reviewer.md`) found that Khoj can split one uploaded
Markdown document into more than one indexed "entry" and return several
hits sharing the same filename - `docs/adr/0003`'s original contract spike
only ever uploaded one small test file and never observed this. Without a
dedup step, the same document could appear twice in a `semantic` page,
`_hybrid`'s RRF loop would add a second score term for it purely because
Khoj chunked it (inflating its fused rank), and a same-channel collision
would be mislabeled as `channels=("exact", "semantic")`; the identical gap
in `AskQuestion._resolve_references` could duplicate a citation. Both are
fixed by deduping on `document_id` (keeping the first, best-ranked
occurrence) before either list is used, with regression tests added
(`tests/unit/test_search_hybrid.py`, `tests/unit/test_ask.py`).

**Negative.** No `semantic`/`hybrid` pagination and no incremental sync are
real, documented gaps, not silent ones - both are naturally scoped follow-ups
once actual usage shows whether they matter at Release 1's corpus size.

## Verification

`tests/unit/test_khoj_export.py`: filename encode/decode round-trips
(including non-ASCII and colon-bearing `stable_key` values, e.g. a daily
digest's ISO timestamp), and `khoj_markdown`'s front matter parses back as
valid YAML with `PyYAML`, not merely as JSON.

`tests/unit/test_search_hybrid.py`: RRF fusion arithmetic against fakes for
`ExactSearchPort`/`KhojPort` (rank-position scoring independent of Khoj's raw
score direction, phrase boost outranking a non-phrase semantic-only hit,
`degraded=True` on `KhojUnavailableError` and on no-Khoj-wired, a stale Khoj
hit with no current-revision match silently dropped, and - added after
independent review - two Khoj hits sharing a filename deduped rather than
double-counted in both `semantic` and `hybrid` modes).

`tests/unit/test_khoj_sync.py`: `SyncKhojIndex` builds the exact filename/
Markdown `khoj_export` would for each `DocumentExportRecord` and calls
`KhojPort.index` once with all of them.

`tests/unit/test_ask.py`: `enabled=False` makes zero calls to `KhojPort`;
`KhojUnavailableError` maps to `degraded=True`; references are resolved and
workspace-verified the same way semantic search's are.

`tests/unit/test_khoj_client_chat.py`: `HttpKhojClient.chat` against a mocked
`httpx` transport - the exact request body sent (`q` prefixed `/notes `, `n`,
`stream=False`, `create_new=True`), correct parsing of the verified response
shape, and a non-string `"response"` raising `KhojUnavailableError`.

`tests/unit/test_discord_commands.py`: `/search`/`/ask` owner-gating, mode
validation, and the `enabled=False`/`degraded=True`/normal-answer reply text
for `/ask`.

`tests/integration/test_document_export_reader.py`: `list_all_current_for_export`
and `PostgresExactSearch.hydrate` against a real PostgreSQL instance.
`tests/integration/test_api_ask.py`, `tests/integration/test_api_admin.py`,
and the `test_api_search.py` additions exercise `/v1/ask`,
`/v1/admin/khoj-sync`, and `/v1/search?mode=semantic|hybrid` through the real
ASGI app against real PostgreSQL, including a real (unreachable-in-this-test-
stack) `HttpKhojClient` so the degrade path is genuine, not mocked.

**All of the above actually run, not just written this slice:**
`uv run ruff format --check .` / `ruff check .` / `mypy` all pass clean;
`pytest -m "not integration and not contract"` (402 passed);
`pytest -m integration` against a real `docker compose --profile core`
PostgreSQL (271 passed); `pytest -m contract` against the real pinned Khoj
container, `--profile ai` (4 passed, the pre-existing `docs/adr/0003` suite,
unaffected by this slice's additive `chat()` method).

**Additionally, live-verified by hand against both real services running
together** (not a permanent automated test - `tests/contract/khoj/` is
deliberately Khoj-only per `docs/adr/0003`, and adding a combined
PostgreSQL+Khoj suite is out of scope for this slice): a synthetic document
was exported through the real `khoj_filename`/`khoj_markdown` and
`SyncKhojIndex`/`HttpKhojClient.index`, then found by
`Search(mode="semantic")` through real `HttpKhojClient.search` and
`parse_khoj_filename`, correctly mapped back to its `document_id` with
`channels=("semantic",)` and a real relevance score - confirming the whole
export/sync/search chain works end to end against live Khoj, not only
against fakes. `HttpKhojClient.chat` was separately confirmed to raise
`KhojUnavailableError` for the real unconfigured-instance failure (see
"Decision" §3 above).

Independent review: `.claude/agents/code-reviewer.md` reviewed this slice's
diff before merge and found the duplicate-Khoj-hit issue documented above,
which was fixed and covered by regression tests before this ADR's commit
(see the pull request for the full findings). Per `CLAUDE.md`'s
Implementation behavior section, this change touches retrieval
and model prompts and therefore also requires Codex's independent evaluation
before merge, which this ADR cannot itself satisfy - see the pull request
description.
