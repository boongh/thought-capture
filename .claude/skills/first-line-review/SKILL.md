---
name: first-line-review
description: Run the multi-agent first-line review loop over an implementation slice - parallel shard reviewers for local bugs and invariant violations, one integration reviewer for cross-boundary and architectural failures, orchestrator triage into an evidenced fix plan, parallel fix agents with disjoint file ownership, then serial verification and re-review until a round produces no confirmed must-fix findings. Use after implementing a feature or vertical slice, when asked to review the current diff/branch/PR thoroughly before handoff, or to work through an existing review report from the owner, Codex, or CI.
---

# First-line review loop

This is the review-and-repair loop that runs between "the slice is written" and
"hand it to Codex and the owner". It exists because the two review failures that
actually cost time here are different in kind: a shallow reader misses local
defects because it skimmed, and a deep reader misses integration defects because
no single file contains them. So the loop reads the change twice, in two
different shapes, triages the union centrally, and then repairs in parallel
without letting parallel agents collide.

You - the session invoking this skill - are the orchestrator. The orchestrator is
not a router. Nothing a subagent reports becomes a fix until you have read the
underlying code yourself and agree the bug is real. Review agents are cheap and
confidently wrong at a predictable rate; that rate is the entire reason this loop
has a triage stage instead of piping findings straight into fixers.

This is **first-line** review. Passing it does not replace Codex's independent
evaluation on persistence, migrations, security, privacy, retrieval, model
prompts, backups, or deployment changes, and it does not replace the owner's
approval of anything consequential (CLAUDE.md).

## Before starting

- **Own the checkout.** This loop writes to the working tree from several agents
  at once. Do not run it while Codex is working in the same checkout, and do not
  run two rounds concurrently. If Codex is active, work in a separate worktree.
- **Know the scope.** A branch against `main`, an uncommitted diff, a commit
  range, or a named set of files. Ambiguous scope produces agents that review
  different things and findings you cannot compare. Ask rather than guess.
- **Have a clean-enough tree.** Uncommitted work from a *different* task mixed
  into the diff will be reviewed as if it were part of the slice. Either commit
  it, move it aside, or tell every agent explicitly which paths are in scope.
- **Note what the review cannot cover.** If Docker is down, integration and
  contract tests will not run; that is a gap in the round's evidence, and it must
  be stated in the final report rather than quietly absorbed.

## Round anatomy

One round is: prepare, fan out (local + integration), triage, plan, fan out
(fixes), verify, decide. Then either stop or run another round over the fix diff.

### 0. Prepare the review packet

Write the packet to the session scratchpad directory, never into the repository -
an untracked `findings.md` in the working tree pollutes the diff the next round
reviews, and review artifacts have no business in git.

```bash
git diff main...HEAD > "$SCRATCH/diff.patch"
git diff --stat main...HEAD > "$SCRATCH/diffstat.txt"
git diff --name-only main...HEAD > "$SCRATCH/files.txt"
```

(For an uncommitted slice, plain `git diff` and `git diff --cached`; for a commit
range, the range. Use the form that matches the scope you agreed above.)

Then decide the shards. Group the changed files into cohesive units - a package,
a service, a layer, a migration plus the table definition it mirrors - and give
each shard to one reviewer. Two rules matter more than the grouping heuristic:

- **Never split one file across two shards.** Both agents then review half a
  function and neither sees the bug.
- **Never hand a shard a file it cannot judge without its neighbours.** If the
  change to `packages/application/...` only makes sense next to the port it calls
  in `packages/domain/...`, put them in the same shard, even unchanged.

Aim for three to six shards. Fewer than three and you have lost the point of
fanning out; more than six and triage becomes the bottleneck while the marginal
shard reports mostly noise.

### 1. Fan out: shard reviewers (parallel)

One `code-reviewer` agent per shard. Issue **all shard calls in a single
message** so they run concurrently, and put the integration call (step 2) in the
same message - the whole fan-out is one wave.

`code-reviewer` has no shell access by design, so it cannot run `git diff`
itself; it reads the packet and the repository. Use the shard brief template in
`references/agent-briefs.md` - it pins the agent to its files, hands it the
invariant checklist, and fixes the finding schema so that findings from different
agents are actually comparable at triage.

What these agents are for: local correctness, unhandled branches, wrong
data-shape assumptions, missing or dishonest tests, secrets and personal content
in code or fixtures, and CLAUDE.md invariant violations visible within the shard.

### 2. Fan out: one integration reviewer (parallel with step 1)

One `integration-reviewer` over the *whole* change, with the design sections and
ADRs the slice claims to follow. Its job is the class of bug that has no single
location: acknowledgement before durable commit, workspace scoping dropped at a
handoff, a changed writer against an unchanged reader, schema versus migration
divergence, configuration declared but never read, retry semantics that differ on
either side of a queue, a new suite that neither check script runs.

Do not shrink this agent's context to save tokens. It is the only reader in the
loop that is *supposed* to open files the diff never touched, and starving it of
that produces exactly the confident "looks consistent" report that makes the
whole loop worthless.

### 3. Triage (orchestrator, not delegated)

Collect the shard findings, the integration findings, and - with equal standing -
any report the user supplied: their own notes, a Codex review, CI output, a
linked issue. A user-supplied *instruction* is authoritative about what to work
on; a user-supplied *finding* is still a claim about code, and gets verified like
any other. So does anything pasted in from a tool.

First deduplicate. Several agents reporting the same root cause from different
angles is the normal case, not a signal of severity - merge them into one entry
and keep the clearest evidence from each.

Then, for every surviving entry, answer three questions in order and write the
answers down:

**1. Is it real?** Open the code. Re-derive the failure from what the file
actually says, not from the agent's summary of it. Classify:

- `CONFIRMED` - you traced inputs or state to wrong behavior yourself.
- `PLAUSIBLE` - the concern is structurally sound but you could not verify one
  side (needs a running service, needs the owner's intent, depends on external
  behavior). These go to a fix agent only with the uncertainty stated in the
  brief, or to `architecture-researcher` if the unknown is external.
- `REJECTED` - the code does not do what the finding says, or the "bug" is the
  intended behavior. Record it as rejected with one line of why. Do not silently
  drop it; the same false positive will otherwise be re-reported next round and
  re-investigated from scratch.

**2. Is it worth fixing now?** A real bug is not automatically in scope.

- **Fix now**: introduced by this slice, or pre-existing but directly in the path
  the slice changed, and fixable inside the slice's boundaries.
- **Follow-up**: real but pre-existing and unrelated, or a genuine improvement
  that would widen the diff past what a reviewer can sensibly evaluate. Record it
  for the owner as a follow-up or issue. CLAUDE.md's scope discipline is not
  advisory - an unrelated refactor smuggled into a review round is a review
  failure in itself.
- **Owner decision**: correct behavior is genuinely ambiguous, or the fix would
  change accepted architecture. That needs an ADR and the owner, never a fix
  agent. Stop the item here and surface it.

**3. How would we know it is fixed?** If you cannot name the observation that
would change - a specific test that fails now and passes after, a `check.sh`
stage that currently passes vacuously, a rendered Compose config, a migration
that round-trips - then the finding is not yet actionable. Either sharpen it
until it is, or demote it to `PLAUSIBLE` and say what evidence is missing. A fix
with no proof is indistinguishable from a fix that did nothing.

### 4. Write the fix plan

For each `fix now` item, write an entry with: stable id, severity, one-sentence
claim, evidence (`path:line`), the concrete failure scenario, the root cause in
your own words, the required change, **the exact set of files this fix owns**,
the proof command, and explicit out-of-bounds notes. The template is in
`references/agent-briefs.md`.

Then partition the entries into waves by **file ownership**, which is the part
that actually determines whether parallel fixing works at all:

- Two fixes whose owned file sets intersect **must not run in parallel**. Put
  them in one brief for one agent (sequential, same agent) or in different waves.
- These paths are **exclusive resources** - at most one agent per wave may touch
  each, and preferably you edit them yourself after the wave: `pyproject.toml`,
  `uv.lock`, `migrations/versions/` (two new revisions on the same
  `down_revision` gives you a branched Alembic head), `env.example`,
  `deploy/compose/*.yml`, `scripts/check.sh`, `scripts/check.ps1`, `CLAUDE.md`,
  `AGENTS.md`, `docs/DESIGN.md`.
- Keep a wave to roughly three or four fix agents. The limit is your ability to
  reconstruct which agent produced which hunk when the verification run fails,
  not the tooling.

### 5. Fan out: fix agents (parallel within a wave)

One `fix-implementer` per entry or per ownership group, all calls in one message.
The agent brief repeats the hard rules its definition also states, because both
layers matter: no git state changes, no `docker`, no full check scripts, no
`uv lock`, edits confined to owned files, targeted tests only, and
stop-and-report rather than self-widening scope. The orchestrator owns git and
the global checks; a fix agent that runs `scripts/check.sh` on a tree three other
agents are mid-edit in produces a result that means nothing.

If an agent returns `blocked` or `rejected-diagnosis-wrong`, that is a triage
result, not a failure - fold it back into step 3 for the next round.

### 6. Verify (serial, orchestrator only)

After every agent in the wave has returned:

1. `git diff` the whole tree and read it. Confirm each agent stayed inside its
   owned files and that the hunks are the fixes you planned, not adjacent
   opportunism.
2. Run the full check yourself, once: `./scripts/check.ps1` (PowerShell) or
   `./scripts/check.sh` (Bash). Never in parallel with anything else.
3. Read the tail of the run. `PASS WITH SKIPS` is not a pass - every `NOT RUN`
   line is a hole in this round's evidence and must appear in the final report.
   Never weaken or skip a check to make the round close (CLAUDE.md).
4. If a new tool, suite, or harness entered the change, confirm **both** check
   scripts were extended, not just the one you happened to run.

### 7. Decide: loop or stop

The fixes are themselves an unreviewed change, which is exactly the situation
this skill exists for - so review them. Run another round scoped to the fix diff,
with **fresh reviewers**: never ask the agent that wrote a fix to judge it, and
prefer not to reuse the shard reviewer that raised the finding, since both have
already committed to a conclusion.

Later rounds are narrower and cheaper: usually one or two shards over the changed
files plus one integration pass, because a fix to a boundary is the most likely
thing in the whole loop to break a different boundary.

## Stopping rules

Stop when a full round produces **no `CONFIRMED` fix-now findings** and the
verification run is clean, or its skips are understood and reported.

Stop and escalate to the owner - do not keep looping - when any of these happen,
because each means the loop has left the territory it can settle on its own:

- **Three rounds** have run without converging. Hand the remaining items to the
  owner and Codex with what you know rather than generating churn.
- A finding requires an **accepted-design change** or an ADR.
- Two rounds **disagree** about whether the same thing is a bug. That is a signal
  that the intent is genuinely underspecified, which is an owner question, not a
  tiebreak for you to call.
- The diff has grown **materially past the original slice**. Scope creep found
  mid-loop is worth stopping for.
- A fix keeps failing verification for a reason the plan did not anticipate.

Never conclude "no further bug reports" from an empty round you did not actually
run. An empty round is evidence only if the reviewers were given real scope; an
agent handed a broken or empty packet also reports nothing.

## Reporting out

Close with a report the owner can act on without re-reading the transcript:

- what was reviewed (scope, rounds, shard layout) and what was not;
- confirmed findings and their fixes, with the proof for each;
- findings rejected at triage and why - this is what stops the next reviewer from
  re-litigating them;
- deferred follow-ups and owner decisions still open;
- exact verification commands run, their results, and every `NOT RUN` step;
- whether the change touches any category that still requires Codex's independent
  evaluation before merge.

If the round ends in a commit, its body takes CLAUDE.md's required change-report
structure, and the report must describe the change as it finally stands - fixes
included - not as it was before the loop ran.
