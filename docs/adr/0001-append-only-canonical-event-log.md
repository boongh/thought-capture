# ADR-0001: Append-only canonical event log

- **Status:** Accepted
- **Date:** 2026-08-30
- **Design anchor:** `docs/DESIGN.md` 3.2, 6.2, 19
- **First implemented in:** `migrations/versions/0001_canonical_capture_layer.py`

## Context

The product goal is that the system remains useful "if every derived artifact
and AI provider disappears" (`docs/DESIGN.md` 2). That guarantee rests entirely
on the raw capture log being trustworthy. If a raw thought can be edited or
deleted — by a future feature, a well-meaning cleanup script, an LLM rewriting
step, or a bug — the durable personal record is no longer durable, and there is
nothing to reconstruct derived documents from.

Application-level discipline is not sufficient. "No code issues an UPDATE on
`thoughts`" is a property that holds until the first person who does not know
the rule writes the first line of code that breaks it.

## Decision

Raw thoughts are append-only, enforced by the database in two independent
layers:

1. **Least-privilege role.** The application connects as `tc_app`, which is
   granted only `SELECT` and `INSERT` on `thoughts`, `blobs`, and
   `thought_attachments`, and holds no `CREATE` privilege on `public`.
2. **Trigger.** `reject_thought_mutation()` fires `BEFORE UPDATE OR DELETE` on
   `thoughts` and raises `restrict_violation`. This stops a mutation even when
   attempted by the migration role or a superuser, which grants alone cannot.

Corrections are new rows linked to the original through `correction_of`, never
edits.

The single documented escape is a restore-only administrative session, which
must set `tc.allow_thought_restore = 'on'`. This exists so that a verified
backup restore can write historical rows; it is deliberately explicit, session
scoped, and absent from all normal code paths.

## Consequences

**Positive.** The invariant is a database guarantee rather than a convention, so
it survives contributors, refactors, and agents that have not read the design.
The escape hatch is greppable, which makes any use of it reviewable.

**Negative.** Restore tooling must opt in explicitly, and any legitimate future
need to mutate a raw row — a deletion feature, for instance — becomes a
deliberate, visible act requiring its own ADR. `docs/DESIGN.md` 12.3 already
requires that.

**Operational.** Migration 0001 creates `tc_app` without a password if absent;
`deploy/compose/initdb/01-roles.sh` provisions it with a password at cluster
creation. Table privileges live in the migration, so they stay versioned with
the schema they protect.

## Verification

`tests/integration/test_canonical_schema.py` asserts that `UPDATE` and `DELETE`
are rejected, that the restore session may proceed, that a correction leaves the
original intact, and that `tc_app` holds neither `UPDATE` nor `DELETE` on
`thoughts` nor `CREATE` on `public`.
