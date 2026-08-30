# ADR-0005: Workspace-scoped, single-user-first schema

- **Status:** Accepted
- **Date:** 2026-08-30
- **Design anchor:** `docs/DESIGN.md` 6, 19
- **First implemented in:** `migrations/versions/0001_canonical_capture_layer.py`

## Context

Release 1 serves exactly one person. The obvious schema for one person has no
tenancy column at all. But two plausible futures both need one:

- isolated personal workspaces, so a second person can use the same deployment;
- shared "hivemind" workspaces, where several members contribute to one brain.

Retrofitting a tenancy key onto an append-only log is unusually painful. The raw
rows cannot be rewritten to carry a new column value, because they are
append-only by ADR-0001. Backfill would require the restore escape, on every
historical row, exactly once, with no margin for error.

## Decision

Every domain table carries `workspace_id` from the first migration. Release 1
seeds one user, one workspace, and one membership.

Identity is modelled separately from the external account that produced a
capture: `users` holds the person, and `external_identities` maps
`(provider, external_user_id)` to a user within a workspace. Discord is one
provider among several possible ones.

Workspace-level configuration that affects behaviour — `timezone` and
`digest_local_time` — lives on the `workspaces` row rather than only in process
configuration, so that a second workspace can differ without a code change.

## Consequences

**Positive.** The two future modes need no re-keying of canonical data. Adding
PostgreSQL row-level security later becomes a policy change over an existing
column rather than a migration of the entire log. Workspace comes from
authentication, never from an untrusted query parameter (`docs/DESIGN.md` 10).

**Negative.** Every query, index, and repository method must carry the workspace
predicate even while exactly one workspace exists — a small, permanent tax paid
against a future that may not arrive. Indexes lead with `workspace_id`
(`thoughts_workspace_time_idx`) accordingly.

**Deferred.** Real authentication, row-level security, invitations, and
per-workspace budgets are Phase 5 and out of scope here. This ADR commits only
to the schema shape, not to multi-user behaviour.

## Verification

`tests/integration/test_canonical_schema.py` seeds a workspace and user and
exercises capture through them; the schema rejects a thought without a valid
`workspace_id` by foreign key.
