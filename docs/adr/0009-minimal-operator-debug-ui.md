# ADR-0009: A minimal, loopback-only operator debug UI

- **Status:** Accepted
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

- **Scope.** Three server-rendered HTML pages - `/debug/thoughts`,
  `/debug/entities`, `/debug/digests` - and the JSON endpoints they call
  (`GET /v1/documents`, `GET /v1/documents/{id}`, `GET /v1/entities`,
  `GET /v1/entities/{id}`, alongside the existing `GET /v1/thoughts`). No
  search, no Ask, no Settings, no revision diffing, no run history screen, no
  write operations of any kind.
- **Where it lives.** Inside the existing `apps/api` gateway, on the same
  loopback-bound port as everything else (`docs/DESIGN.md` 12.2), following
  the exact router/reader pattern `routers/thoughts.py` already established
  rather than inventing a second web stack.
- **No new dependency.** Pages are hand-built HTML with `html.escape` on every
  interpolated value, not a templating engine - the surface is three static
  layouts, not a growing set of views.
- **Auth.** The JSON endpoints require the existing `Authorization: Bearer`
  header, unchanged. The three HTML pages accept that header *or* a `?token=`
  query parameter, because a plain browser tab cannot set a custom header. The
  query-param path is deliberately narrow - it exists only on `/debug/*`, never
  on `/v1/*` - and is an acceptable relaxation specifically because the port is
  loopback-only: the token in the URL bar never leaves the operator's own
  machine.
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

**Negative.** A `?token=` query parameter can end up in shell history or a
browser's URL bar autocomplete on the operator's own machine. Accepted because
the port never leaves loopback and the alternative (no browsable debug view at
all) fails the request this ADR exists to satisfy.

**Deferred.** Pagination on `/debug/entities` and `/debug/digests` follows
`PostgresEntityReader.list_entities`'s existing precedent (unpaginated up to a
limit): a personal corpus's document and entity counts are small. `/debug/thoughts`
reuses `PostgresThoughtReader.list_thoughts`'s real keyset pagination, since
the raw log is not bounded the same way.

## Verification

`tests/unit/test_debug_templates.py` and `tests/integration/test_document_reader.py`
cover the new reader and HTML-escaping directly; `tests/integration/test_api_documents.py`
and `test_api_entities.py` cover the new JSON endpoints and the debug pages'
dual auth path end to end against a real Postgres instance.
