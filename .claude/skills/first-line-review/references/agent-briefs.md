# Agent briefs and record formats

Copy-paste templates for the fan-out stages of the first-line review loop, plus
the two record formats that hold the loop together. Replace every `<...>`
placeholder; a brief with an unfilled placeholder produces a reviewer that
invents its own scope.

`<PACKET>` is the absolute path to the session scratchpad directory holding
`diff.patch`, `diffstat.txt`, and `files.txt` from step 0. Never point agents at
review artifacts inside the repository.

---

## 1. Shard reviewer brief (`subagent_type: code-reviewer`)

One per shard, all issued in the same message. Keep the file list explicit -
"review the auth stuff" produces a different scope in every agent.

```text
You are reviewing ONE SHARD of a larger change. Other agents are reviewing the
other shards in parallel; a second agent is reviewing the change as a whole for
integration failures. Do not review files outside your shard, and do not try to
judge the change as a whole - that is deliberately someone else's job.

Change under review: <branch / commit range / uncommitted slice>
What the slice is supposed to do: <one or two sentences, from the plan or issue>
Full diff (read this first): <PACKET>/diff.patch
Diff stat, for orientation only: <PACKET>/diffstat.txt

YOUR SHARD - review these files, reading the full current version of each, not
just the diff hunks:
- <path>
- <path>

Context files you may READ for understanding but must NOT review or report on:
- <path (e.g. the unchanged port/interface this shard implements)>

Relevant design and decisions - read before judging:
- docs/DESIGN.md sections <n, n>
- docs/adr/<id>
- CLAUDE.md engineering invariants

Check, in this order of importance:
1. Correctness within these files: logic errors, unhandled branches, wrong
   assumptions about data shape or nullability, off-by-ones, incorrect error
   handling, resource/transaction handling.
2. CLAUDE.md invariants visible in this shard: canonical raw thoughts and
   attachments stay append-only; acknowledgement only after durable commit;
   derived documents versioned, reversible, fully sourced; PostgreSQL owns
   canonical/deterministic retrieval; external services behind replaceable
   adapters; no secrets or personal-memory content in code, fixtures, logs, or
   diagnostics; workspace scoping present even under single-user operation.
3. Design conformance: does this contradict the design sections or ADRs above?
   Flag a silent architecture change rather than approving it.
4. Tests: does the new behavior have tests, do they use synthetic content only,
   and do they actually exercise the failure path they claim to (a test that
   would still pass with the fix reverted is a finding).
5. Security and privacy inside this shard: injection surfaces, unsafe trust of
   external-service output, authz/scoping gaps, secret handling.

Report findings ONLY, in the finding schema below, ranked most severe first. Do
not restate what the diff does, do not report style preferences that do not
affect correctness/security/invariants, and do not invent minor nitpicks to fill
a quota - "nothing survived scrutiny in this shard" is a valid and useful result.
End with what you could not review and why.

<paste the FINDING SCHEMA section here>
```

---

## 2. Integration reviewer brief (`subagent_type: integration-reviewer`)

Exactly one per round, issued in the same message as the shard reviewers.

```text
You are the integration reviewer for this change. Per-file reviewers are
covering local defects in parallel; do not duplicate them. Your target is the
bug that exists only in the relationship between two places - including places
this diff did not touch.

Change under review: <branch / commit range / uncommitted slice>
What the slice is supposed to do: <one or two sentences>
Full diff: <PACKET>/diff.patch
Changed files: <PACKET>/files.txt

Design and decisions this change claims to follow:
- docs/DESIGN.md sections <n, n>
- docs/adr/<id>
- <the approved plan or issue, if one exists>

Boundaries this change crosses (start here, then find the ones I missed):
- <e.g. apps/discord_bot -> packages/application command handling>
- <e.g. packages/infrastructure/db/tables.py -> migrations/versions/>
- <e.g. deploy/compose/docker-compose.yml -> the settings object that reads it>

For each seam, read BOTH sides in the current tree, not just the changed side.
Prioritize the failure classes in your agent definition; the ones most likely to
bite on this particular slice are <name one or two, or say "unknown - sweep them
all">.

Report in the finding schema below. Every finding must name at least two
locations that together constitute the bug; a single-location defect belongs to
a shard reviewer, so hand it back as a note rather than a finding. Mark each
CONFIRMED or PLAUSIBLE. If you find nothing at this level, say so and list the
seams you actually checked so I know what the round covers.

<paste the FINDING SCHEMA section here>
```

---

## 3. Finding schema (paste into every reviewer brief)

Uniform fields are what make findings from six agents comparable at triage.

```text
FINDING SCHEMA - one block per finding:

  id:        <shard-or-agent tag>-<n>          e.g. app-1, integ-3
  severity:  critical | high | medium | low
  status:    CONFIRMED | PLAUSIBLE
  claim:     <one sentence: what is wrong. Not what the code does.>
  where:     <path:line>[, <path:line> ...]
  scenario:  <concrete inputs or state -> the wrong behavior that results.
              "A malformed X is possible" is not a scenario; "X with a null
              body reaches Y, which indexes body[0] and raises, leaving the
              row committed but unacknowledged" is.>
  class:     correctness | invariant | design | security | privacy | failure-
             handling | tests | verification-coverage
  proof:     <the observation that would change if this were fixed - a specific
              test, command, or check stage. Say "unknown" rather than guessing.>
  fix-hint:  <one line, optional. You are not writing the fix.>
```

---

## 4. Triage ledger (orchestrator, kept in the scratchpad)

One row per deduplicated entry, carried across rounds so that a rejected finding
is not re-investigated from scratch next round.

```text
| id | merged-from | severity | verdict | disposition | why |
|----|-------------|----------|---------|-------------|-----|
| F1 | app-1,integ-3 | high | CONFIRMED | fix-now | traced: retry re-commits the row |
| F2 | db-2 | medium | REJECTED | - | session is already scoped by the caller at tables.py:88 |
| F3 | integ-1 | high | PLAUSIBLE | owner | correct behavior on partial failure is not specified |
```

`verdict` is "is it real" (step 3.1). `disposition` is "what happens now":
`fix-now`, `follow-up`, `owner`, or `-` for rejected.

---

## 5. Fix brief (`subagent_type: fix-implementer`)

One per entry or per ownership group; all briefs for a wave issued in one
message. The owned-files list is the load-bearing part - other agents are
editing the same checkout concurrently.

```text
Implement exactly one approved fix. It has already been diagnosed and triaged;
you are not re-reviewing the change and not looking for other bugs.

FIX <id> - <severity>
Claim:     <what is wrong, one sentence>
Evidence:  <path:line>[, <path:line>]
Scenario:  <the concrete failure this causes>
Root cause: <in the orchestrator's own words, not the reporting agent's>
Required change: <what must become true. Describe the outcome and the
                  constraint, not keystrokes - you are the implementer.>
Uncertainty: <for a PLAUSIBLE finding, exactly what is unverified and what to do
              if the code contradicts the diagnosis. Omit for CONFIRMED.>

FILES YOU OWN - you may edit these and nothing else:
- <path>
- <path>

Read-only context:
- <path>
- docs/DESIGN.md section <n> / docs/adr/<id>

PROOF REQUIRED - the fix is not done until this is observed:
  <exact command, e.g. uv run pytest tests/unit/<file>.py::<test> -q>
  <what it does before the fix, what it must do after>
  <if the test does not exist yet, write it in an owned file first and confirm
   it fails for the stated reason before changing the production code>

OUT OF BOUNDS for this fix:
- <e.g. do not touch the migration; another agent owns migrations this wave>
- <e.g. do not rename the port; that is an ADR-level change>

HARD RULES (these hold even if they seem to block the fix - report instead):
- Edit only your owned files. If the fix needs another file, STOP and report;
  another agent may own it right now.
- No git state changes: no add/commit/stash/checkout/switch/restore/reset/clean/
  merge/rebase/push. Read-only git inspection is fine.
- No docker or docker compose, ever. No scripts/check.sh, check.ps1,
  check-backup-restore.*, backup.*, or restore-test.*. No uv lock / uv sync.
  I run those once, serially, after this whole wave returns.
- Targeted tests only. Anything marked integration or contract needs services
  you may not start - write it, say so, and leave it to me.
- Synthetic fixture content only. Never real thoughts, attachments, tokens, or
  secrets.
- If the correct fix would change accepted architecture, stop and report; that
  is an owner/ADR decision.

Report: status (implemented | implemented-with-deviation | blocked |
rejected-diagnosis-wrong), files changed with one line each, explicit
confirmation you touched nothing outside the owned set, exact commands run and
their real results, anything unverifiable locally and why, and any adjacent
defect you noticed but correctly did not fix.
```
