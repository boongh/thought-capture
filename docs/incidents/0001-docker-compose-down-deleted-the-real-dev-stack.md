# Incident 0001: `docker compose down -v` deleted the real local dev stack

- **Date:** 2026-09-08
- **Severity:** High (data deletion), Low (actual impact — the deleted data was recent, low-value development/test capture; the owner considers it fully recoverable by resending the same messages on Discord). Recorded at High severity anyway: the *mechanism* was capable of deleting genuinely important data and will recur verbatim on the next long agent session unless closed here, not just remembered.
- **Actor:** Claude (Claude Code), acting autonomously during manual verification of PR #31 (`docs/adr/0010` Slice 1, embedding sidecar).
- **Who found it:** Claude, immediately after running the command, before the user noticed anything.

## What happened

While manually verifying that `deploy/compose/docker-compose.yml`'s `embedding-sidecar` service no longer publishes a host port by default (the fix for a separate Codex review finding), two throwaway compose stacks were brought up and torn down in sequence:

1. **First verification** (correct): `docker compose -p sidecar-noport-test -f deploy/compose/docker-compose.yml --profile core up -d --build embedding-sidecar`, torn down afterward with an explicit `-p sidecar-noport-test`. Isolated, no impact.
2. **Second verification** (the mistake): to test the *with-overlay* case, the stack was brought up again with `docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/embedding-sidecar.contract-test.docker-compose.yml --profile core up -d --build embedding-sidecar` — **no `-p` flag this time**. Compose silently fell back to the project name declared in `docker-compose.yml`'s own `name: thought-capture` directive.

Cleanup for that second verification ran:

```bash
docker compose -f deploy/compose/docker-compose.yml \
  -f deploy/compose/embedding-sidecar.contract-test.docker-compose.yml \
  --profile core down -v
```

Also with no `-p`. Because the project name defaulted to `thought-capture` — the exact same project name the user's own real, already-running, multi-day-old local development stack used — this command matched and tore down that real stack instead of (or in addition to) the throwaway one: it stopped and removed `thought-capture-postgres-1`, `thought-capture-api-1`, `thought-capture-worker-1`, and `thought-capture-discord-bot-1`, and — because of `-v` — deleted the `thought-capture_postgres-data` and `thought-capture_attachments` named volumes. `docker volume rm` (which is what `-v` triggers here) does not soft-delete; once the command returns, the data is gone from the volume driver's backing storage.

This was caught immediately (the very next command run was a reachability check that unexpectedly failed, prompting an inspection of `docker ps -a`/`docker volume ls`, which showed the real stack and its volumes were gone) and disclosed to the user in the same turn, before any further disk-writing commands were run.

## Why it happened

The immediate cause is simple: an unscoped `docker compose down -v` was run against a machine that already had a same-named, real project running.

The more useful question is why a session that had *already* correctly handled this exact risk many times over did not handle it the one time that mattered:

- Early in the same session, `docker ps -a` was run and explicitly noted the real stack's containers (`thought-capture-postgres-1` among them, 5 days old). Every subsequent manual Postgres/sidecar verification in that session — and there were dozens across the conversation — used deliberately distinct container names (`pgvector-recheck`, `embedding-sidecar-final`, etc.) and non-default host ports (15432+) specifically to avoid colliding with that real stack.
- That discipline was applied consistently to plain `docker run` invocations, but not carried over to the two `docker compose up`/`down` invocations near the end of the session. Compose has its own, different default-scoping mechanism (project name, not container name/port), and the habit that had been reinforced for `docker run` did not automatically generalize to it.
- `docker compose down -v` gives no feedback, preview, or confirmation before acting — it silently matches whatever is running under the resolved project name, including containers that predate the current session and that the current session did not start.
- Nothing in this project's own safety guidance (`CLAUDE.md`, or the assistant's own operating instructions) named Docker volume deletion specifically. The existing "check git status before a destructive git command" habit is git-specific; there was no equivalent trained reflex for "check what's running before a destructive Docker command."

This is the same category of failure `docs/adr/0001` already named for a different mechanism: *"Application-level discipline is not sufficient. '\[Nobody does the dangerous thing]' is a property that holds until the first person — or process — who does not know the rule does the dangerous thing."* That ADR's answer was to make the invariant a database-enforced guarantee (role grants plus a trigger), not a convention. The same logic applies here: remembering not to run unscoped teardown commands is necessary but not sufficient, especially deep into a long session under real pressure (in this case, mid–incident-response for an unrelated, already-multi-round security review).

## What actually mitigated the damage

- The deleted data was recent, low-value local development/test capture, not the user's real long-term memory archive. The user considers it recoverable by resending the same messages through the Discord bot.
- The failure was caught within one tool call (the very next reachability check surfaced the problem), not silently, and disclosed immediately and completely rather than after further investigation or additional destructive commands.

Neither of these is a control that can be relied on next time. They were luck (low-value data, small blast radius, an unrelated command surfaced the problem quickly) rather than anything that would have caught this before it happened.

## Prevention — proposed, pending the owner's review

Two independent layers, matching `docs/adr/0001`'s own "convention is not enough, enforce it mechanically" shape:

1. **A guarded wrapper for compose teardown** (`scripts/compose-teardown.sh` / `scripts/compose-teardown.ps1`, added alongside this record): refuses to run at all without an explicit `-p <project-name>`, and refuses outright — no override flag, no bypass — to run a volume-destroying teardown (`-v`/`--volumes`) against this repo's real project name (`thought-capture`, matching `docker-compose.yml`'s own `name:` field). This is the mechanical guarantee: even under time pressure, mid-session, many tool calls into an unrelated task, the exact command that caused this incident is refused by the tool itself rather than depending on the operator (human or agent) remembering to add `-p`.
2. **An explicit, named policy in `CLAUDE.md`** (added alongside this record) requiring any throwaway/manual Docker Compose verification to use `scripts/compose-teardown.sh`/`.ps1` (or an explicit, unique `-p`) rather than bare `docker compose down`, and requiring a read-only inspection (`docker ps -a` / `docker compose -p <name> ps`) immediately before any command that removes containers or volumes, in the same turn as the destructive command — not relying on an inspection run earlier in a long session.

Deliberately **not** proposed: disabling or gating `docker compose down` globally, or requiring interactive confirmation for every Docker command. Both would blunt a tool this project's own workflow (and this session's own extensive manual container verification) genuinely depends on, for a failure mode that a narrowly-targeted guard fully closes. The guard is scoped to exactly the one command shape that caused this incident (`down` plus a volume flag plus the real project name), not Docker or Compose in general.

## Open question for the owner

Whether `scripts/compose-teardown.sh`/`.ps1`'s refusal should be a hard stop with no override (as drafted), or should support an explicit, loudly-worded override flag for the rare legitimate case of intentionally wiping the real dev stack's data. Drafted as a hard stop for now — the underlying `docker compose -p thought-capture down -v` command is still available directly for that case, unwrapped, so nothing is actually prevented, only made to require a deliberate, explicit choice rather than an easy default.
