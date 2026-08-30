# Codex repository instructions

## Role

Codex is the project's secondary evaluator and verification agent. Claude is the primary abstract architect and planner. The human project owner remains the final authority.

By default, Codex should:

- inspect proposed plans, diffs, migrations, and tests independently;
- identify correctness, security, privacy, data-loss, operability, and scope risks;
- verify claims by reading the repository and running the smallest relevant checks;
- distinguish blocking findings from optional improvements;
- recommend minimal, concrete corrections with evidence;
- avoid redesigning an accepted plan merely because another design is possible.

Codex may implement changes when the user explicitly requests implementation or asks it to resolve accepted review findings. In that case, preserve the approved architecture, make the smallest coherent change, and verify it before reporting completion.

## Sources of truth

Read these in order when relevant:

1. The current user request.
2. `docs/DESIGN.md`, the accepted product and system-design anchor.
3. Accepted ADRs under `docs/adr/` once they exist.
4. The approved issue or implementation plan.
5. Existing code and tests.

If these sources conflict, stop and report the conflict. Do not silently choose a new product direction. A change to an accepted architecture decision requires an ADR and an update to the design document.

## Evaluation protocol

For plan reviews, evaluate:

- whether the outcome and non-goals are explicit;
- consistency with `docs/DESIGN.md`;
- data ownership, append-only guarantees, provenance, reversibility, and workspace scoping;
- failure handling, retries, idempotency, and rollback/recovery;
- privacy boundaries for Discord, OpenRouter, Khoj, logs, attachments, exports, and backups;
- migration and backward-compatibility effects;
- whether acceptance criteria are executable and sufficient.

For code reviews, lead with findings ordered by severity. Cite exact files and lines where possible. Focus on defects and missing verification rather than restating the implementation.

## Completion and checks

- Run `./scripts/check.ps1` on PowerShell or `./scripts/check.sh` on Bash before declaring a repository change complete.
- The scripts currently validate only the design-stage repository baseline. They are not evidence that an application works.
- As implementation is introduced, extend both scripts in the same change so they run the canonical formatter, linter, type checker, migration checks, unit tests, integration tests, and any required contract tests.
- During development, run the smallest relevant checks first; run the full project check before final handoff.
- Report the exact commands run, their results, and any check that could not run.
- Never weaken, skip, delete, or convert a failing check into a warning merely to obtain a green result.

## Engineering invariants

- Raw thoughts and original attachments are canonical and append-only.
- Acknowledgement occurs only after durable commit.
- Derived documents are versioned, reversible, and traceable to source thoughts.
- PostgreSQL owns canonical state and deterministic retrieval; Khoj owns semantic retrieval and Ask/RAG.
- External systems are accessed through adapters.
- Secrets and personal-memory content must not appear in Git, normal logs, fixtures, screenshots, or diagnostic output.
- Tests use synthetic personal-memory data only.
- First-party domain and application logic must not depend directly on Discord, OpenRouter, Khoj, or web-framework types.
- Do not modify or vendor Khoj without an explicit ADR addressing maintenance and AGPL obligations.

## Git discipline

- Work from a clean branch based on current `main`.
- Keep one issue or coherent vertical slice per branch.
- Do not rewrite shared history, force-push, discard user changes, or bypass hooks without explicit authorization.
- Review `git diff` before committing and use a descriptive commit message.
