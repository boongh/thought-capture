# ADR-0009: A minimal, loopback-only operator debug UI

- **Status:** Accepted (auth mechanism corrected before merge - see Amendment;
  scope extended to a fourth, read-only page - see Amendment 2)
- **Date:** 2026-09-02
- **Design anchor:** clarifies `docs/DESIGN.md` 2.2, 4.4; extends 4.4's `/v1` gateway
- **First implemented in:** the debug-UI slice (`apps/api/src/tc_api/routers/debug.py`)

## Context

The project owner asked for "some kind of clean and basic/debug UI that would
let me see entities, raw logs with metadata, and digests" to verify pipeline
correctness while the organize/entity-resolution/digest-delivery slices land.

`docs/DESIGN.md` 2.2 lists "a custom browser UI" as a Release-1 non-goal, and
4.4 describes a distinct, later thing: a polished, multi-screen "future unified
interface" (Capture History, Daily Digests, Entities, Search, Ask, Revision
History/Diff, Runs, Settings) that implements semantic search and Ask, and is
explicitly deferred. Building that now, or treating this request as an early
start on it, would silently expand Release 1's scope in exactly the way 2.2 was
written to prevent.

Those two things are not the same request. The owner needs a way to *look at*
what the pipeline already produced - not a product surface for anyone else to
use, not search, not Ask, not a step toward 4.4. Per this repository's own
process for a request that touches accepted architecture, the conflict is
surfaced here rather than resolved silently.

## Decision

Build a minimal, read-only, loopback-only operator inspection tool, not the
4.4 UI:

- **Scope.** Four server-rendered HTML pages - `/debug/thoughts`,
  `/debug/entities`, `/debug/digests`, `/debug/runs` (added by Amendment 2) -
  and the JSON endpoints they call
  (`GET /v1/documents`, `GET /v1/documents/{id}`, `GET /v1/entities`,
  `GET /v1/entities/{id}`, alongside the existing `GET /v1/thoughts`). No
  search, no Ask, no Settings, no revision diffing, no write operations of
  any kind. `/debug/runs` is call-metadata inspection, not the "run history
  screen" this ADR originally excluded - see Amendment 2 for why that
  distinction holds.
- **Where it lives.** Inside the existing `apps/api` gateway, on the same
  loopback-bound port as everything else (`docs/DESIGN.md` 12.2), following
  the exact router/reader pattern `routers/thoughts.py` already established
  rather than inventing a second web stack.
- **No new dependency.** Pages are hand-built HTML with `html.escape` on every
  interpolated value, not a templating engine - the surface is three static
  layouts, not a growing set of views.
- **Auth.** The JSON endpoints require the existing `Authorization: Bearer`
  header, unchanged. The three HTML pages use HTTP Basic Auth instead - see
  the Amendment below for why, and why the query-parameter design this ADR
  originally accepted does not appear here.
- **Design version.** `docs/DESIGN.md`'s version line moves to 1.6 to record
  that 4.4's future item is unaffected and 2.2's non-goal is read as covering
  a product-facing UI, not this tool.

## Consequences

**Positive.** The owner (and Codex, reviewing screenshots or a running
instance) can verify entity resolution, digest content, and raw capture
metadata visually, without `psql`. It costs no new dependency and reuses the
same authentication, readers, and router pattern already accepted for
`/v1/thoughts`.

**Negative.** Two things now read "debug UI" in this codebase's history: this
tool, and 4.4's eventual product UI. A future contributor implementing 4.4 must
not extend these three pages instead of building the real thing against a
stable coordinator API - 4.4's own text already requires that, and this ADR
does not relax it.

**Deferred.** Pagination on `/debug/entities` and `/debug/digests` follows
`PostgresEntityReader.list_entities`'s existing precedent (unpaginated up to a
limit): a personal corpus's document and entity counts are small. `/debug/thoughts`
reuses `PostgresThoughtReader.list_thoughts`'s real keyset pagination, since
the raw log is not bounded the same way.

## Amendment: the `?token=` query parameter was replaced with HTTP Basic Auth

Caught in PR review, before this ADR's original design ever shipped.

**The flaw.** The original decision reasoned that a query-param token was
safe because the loopback-only bind means "the token in the URL bar never
leaves the operator's own machine." That reasoning addressed network
exposure only. It missed a second disclosure path on the *same* machine:
`apps/api/src/tc_api/__main__.py` runs uvicorn with `access_log=True`, and
uvicorn's default access log records the full request line - path and
query string included - to stdout/the container's logs on every request.
A `?token=<secret>` debug request therefore wrote the operator's bearer
token into normal application logs, verbatim, on every page load -
exactly what `docs/DESIGN.md` 14.2 forbids ("Never include raw bodies,
generated documents, model prompts, API keys, or signed attachment URLs
in logs") and unrelated to whether the port ever left loopback. A log
file is itself a disclosure surface: read by anyone with filesystem
access, potentially shipped to aggregation, retained past the request
that created it.

**The fix.** `require_debug_access` (`apps/api/src/tc_api/dependencies.py`)
now accepts HTTP Basic Auth only, checked against the same
`TC_API_BEARER_TOKEN` (the username is ignored - only the password is
checked, like `Bearer <token>`'s scheme name is ignored beyond the
keyword). A missing or invalid credential returns `401` with
`WWW-Authenticate: Basic realm="thought-capture-ai debug"`, which is what
makes a plain browser tab show its own native login prompt without any
JavaScript - the same problem the query-param design was solving, without
the query string. The browser then caches the credential per-origin for
the session and sends it as a header on every subsequent request,
including the `/debug/thoughts` pagination and nav links - none of which
need to carry a secret anymore, so `debug_templates.py`'s `token`
threading was removed along with it. Basic Auth credentials travel in a
header, which the access log line does not include, closing the leak.

The three `/debug/*` pages also now return `Cache-Control: no-store`: they
render personal memory content (raw thought bodies, generated documents),
and a browser or intermediary proxy caching that on disk would be its own
disclosure, independent of the auth question.

`Authorization: Bearer` (the `/v1` JSON scheme) is deliberately *not*
also accepted on `/debug/*` - the two surfaces use distinct schemes on
purpose, so a change to one's auth handling can never silently widen the
other's.

## Amendment 2: `/debug/runs` - call-metadata inspection, not the excluded "run history screen"

Added 2026-09-02, alongside `reasoning_effort` support in `LLMRequest`/
`OpenRouterProvider` (a model can spend its whole output budget on hidden
reasoning and return nothing at all - see
`docs/model-evaluation-organize-select.md`'s GLM 5.3 Flash finding). The
owner asked to be able to see which models were tested with which
reasoning-control setting, from the debug UI, without needing `psql`.

**The conflict.** This ADR's original Scope explicitly lists "no run history
screen" among what a future 4.4-style UI would add, and its Consequences
warn against extending these three pages instead of building 4.4's real
thing. A fourth page is scope growth this ADR deliberately bounded against.
Per this repository's process for a request that touches accepted
architecture (`CLAUDE.md`), that conflict was surfaced to the owner rather
than resolved silently - the owner chose to add a **read-only** view and
explicitly declined a write/settings control (a `reasoning_effort` override
UI), keeping ADR-0009's "no Settings, no write operations of any kind"
intact.

**Why this is not the excluded "run history screen."** 4.4's future "Runs"
item (`docs/DESIGN.md` 4.4) is a product surface: revision history, diffing,
presumably re-running or annotating a run. `/debug/runs` does none of that -
it is one more read-only table over one more existing reader, following the
exact pattern the other three pages already established (`PostgresLlmCallReader`
mirrors `PostgresThoughtReader`'s keyset-pagination shape; `debug_templates.runs_page`
mirrors `thoughts_page`'s hand-built, `html.escape`-everywhere HTML). What it
adds is visibility into *request-shape metadata* - which model was asked,
what reasoning control was sent, cost, latency, error code - the same
category of "verify pipeline correctness without `psql`" this ADR's Context
already accepted for thoughts/entities/digests, extended to the calls that
produced them. It deliberately excludes `request_messages` and
`response_raw` (raw prompt/completion content - personal memory text) from
what it renders, for the same disclosure reasons the other three pages exist
in the first place.

**What was *not* added.** No control to set or change `reasoning_effort` (or
any other request parameter) from this UI. No page lets an operator trigger,
retry, or annotate a run. `Cache-Control: no-store` applies to `/debug/runs`
too, same rationale as the other three pages.

## Verification

`tests/unit/test_debug_templates.py` and `tests/integration/test_document_reader.py`
cover the new reader and HTML-escaping directly; `tests/integration/test_api_documents.py`
and `test_api_entities.py` cover the new JSON endpoints; `tests/integration/test_api_debug.py`
covers Basic Auth (missing, wrong, correct, username-ignored, the old Bearer
and query-token schemes now rejected) and the `Cache-Control: no-store`
header end to end against a real Postgres instance. Manually verified
against a rebuilt `api` container: a wrong or missing credential returns
`401` with `WWW-Authenticate: Basic ...`; the correct credential returns
`200`; the container's own access log shows no credential on any request
line (only an attempted `?token=` request - itself now always `401` -
would still write its value to the log, which is exactly the scenario this
amendment removes as a valid way to authenticate at all).

`/debug/runs` (Amendment 2): `tests/unit/test_debug_templates.py` covers
`runs_page` directly (reasoning-effort display, the empty-state placeholder,
error-code escaping, pagination link presence); `tests/integration/test_api_debug.py`
seeds a real journaled call via `PostgresRunLedger` + `PostgresLLMJournal`
and asserts it renders end to end, plus the shared auth/no-store assertions
reused from the other three pages. `tests/unit/test_openrouter_provider.py`
covers `reasoning_effort` being omitted by default, sent when set, and
journaled into `request_params` either way.
