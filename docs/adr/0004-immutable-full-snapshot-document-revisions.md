# ADR-0004: Immutable full-snapshot document revisions

- **Status:** Accepted
- **Date:** 2026-08-31
- **Design anchor:** `docs/DESIGN.md` 3.3, 3.4, 6.3, 19
- **First implemented in:** `migrations/versions/0004_derived_documents_and_provenance.py`

## Context

Derived documents evolve: an entity document is rewritten as new thoughts
mention it. Two representations are possible.

Storing **diffs** is compact, but reconstructing any historical state requires
replaying every diff from the beginning. A single corrupt or lost diff makes
every later state unrecoverable, and "restore the version from three weeks ago"
becomes an operation whose cost grows with the corpus.

Storing **full snapshots** costs more disk. Entity document bodies are 500-1500
tokens (`docs/DESIGN.md` 7.3), so a document revised daily for a year is a few
megabytes of text — negligible against the value of the guarantee.

## Decision

Every revision stores the complete `body_markdown` of the document at that
point, together with its `body_sha256`, the `run_id` that produced it, its
`parent_revision_id`, a `change_summary`, and a `change_kind`.

Revisions are **immutable**, enforced the same way raw thoughts are (ADR-0001):
a `BEFORE UPDATE OR DELETE` trigger on `document_revisions` raises
`restrict_violation`, and the application role holds only `SELECT` and `INSERT`.

**Reversion is forward motion.** Restoring a historical state does not resurrect
or rewrite the old revision. It appends a *new* revision whose
`parent_revision_id` is the current revision and whose body matches the selected
historical one, with `change_kind = 'manual_restore'`. The chain therefore
records that a restore happened, and the restore is itself revertible.

`documents.current_revision_id` is a foreign key to `document_revisions`, so the
pointer can never reference a revision that does not exist.

## Consequences

**Positive.** Any historical state is one row read, not a replay. Diffs are
computed on demand from two snapshots and are a derived, cacheable artifact
rather than canonical data. The audit trail cannot be rewritten to hide what a
model produced. Restoring is safe because it destroys nothing.

**Negative.** Storage grows with revision count rather than with change size.
`docs/DESIGN.md` 7.3.4 invariant 5 is the mitigation that matters: only *touched*
documents are regenerated, so an untouched document keeps its current revision
and writes no new row. That invariant is described in the design as the largest
single cost lever in the pipeline, and it is a cost lever precisely because
revisions are full snapshots.

**Operational.** Because bodies are complete, the Markdown export and the Khoj
index can both be rebuilt from PostgreSQL alone, which is what makes
`docs/DESIGN.md` Appendix B's "current Markdown can be rebuilt deterministically"
achievable.

## Verification

`tests/integration/test_derived_schema.py` asserts that a revision cannot be
edited or deleted, that revision numbers are unique per document, that a restore
appends a third revision whose parent is the second and whose body matches the
first, and that `current_revision_id` cannot point at a non-existent revision.
