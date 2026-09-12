# Operating and inspection

How to run the currently implemented local stack and see what the system has
actually stored. Written for the project owner.

## Starting the stack

Install Docker Desktop and ensure its engine is running. For host-side
development and checks, also install `uv` and the pinned Python version as
described in [DEVELOPMENT.md](DEVELOPMENT.md).

From the repository root, copy `env.example` to `.env` if needed. Set unique
values for `POSTGRES_PASSWORD`, `TC_APP_DB_PASSWORD`, and
`TC_API_BEARER_TOKEN`; keep the database URL passwords in sync with their
corresponding password variables. The full `core` profile also requires
`TC_DISCORD_BOT_TOKEN` and `TC_DISCORD_OWNER_USER_ID`. Never paste or commit
these values.

Database passwords are interpolated into SQLAlchemy URLs while the same raw
values provision PostgreSQL, so the current Compose stack requires ASCII
letters and digits only. Characters such as `/`, `@`, `#`, and `%` are not
supported. Generate each database password separately with an alphanumeric-only
password-manager profile or:

```bash
python -c "import secrets,string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32)))"
```

For a fresh Discord bot:

1. Create the bot in the Discord Developer Portal, copy its token into
   `TC_DISCORD_BOT_TOKEN`, and enable the privileged **Message Content Intent**.
2. Enable Developer Mode in the Discord client and copy your user ID into
   `TC_DISCORD_OWNER_USER_ID`.
3. For private-channel capture, invite the bot with View Channel, Send Messages,
   and Read Message History permissions, then set both `TC_DISCORD_GUILD_ID` and
   `TC_DISCORD_CHANNEL_ID`. Leave both blank for DM-only capture.

The API readiness endpoint does not test the Discord Gateway connection. Confirm
the bot reached `discord.ready` in `discord-bot` logs after startup.

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yml --profile core up -d --build
```

`--env-file .env` is required. Compose resolves a bare `.env` relative to the
compose file, not the repository root. The `migrate` service applies Alembic
migrations and seeds the configured workspace before the long-running services
start; do not run a second migration command for a normal Compose startup.

The `core` profile currently starts:

| Service | Purpose |
| --- | --- |
| `postgres` | Canonical PostgreSQL store |
| `migrate` | One-shot schema migration and workspace/owner seed |
| `api` | Health endpoints and authenticated raw-thought capture/read API |
| `discord-bot` | Allowlisted Discord text and attachment capture |
| `worker` | Closed-window organization scheduler and catch-up worker |

Check status and readiness:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yml --profile core ps
curl http://127.0.0.1:8080/health/ready
```

Follow startup or failure logs without printing the contents of `.env`:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yml --profile core logs -f migrate api discord-bot worker
```

The interactive API documentation is at <http://127.0.0.1:8080/docs>.
`/health/live` and `/health/ready` are intentionally unauthenticated; `/v1`
and `/debug` routes require bearer authentication. Send
`Authorization: Bearer <TC_API_BEARER_TOKEN>` from an authenticated client;
do not place a reusable bearer token in a URL or a log.

Stop the services while preserving the PostgreSQL and attachment volumes:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yml --profile core down
```

## Opening a database shell

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yml exec postgres psql -U tc_migrator -d thought_capture
```

That command assumes the `POSTGRES_USER=tc_migrator` and
`POSTGRES_DB=thought_capture` defaults. Substitute the configured values if you
overrode either setting.

`tc_migrator` owns the schema and can read everything. The services connect as
`tc_app`, which can append to the capture tables and read the identity tables,
but cannot modify or delete a thought (ADR-0001).

## Where captured messages land

A Discord message becomes one row in `thoughts`. This is the canonical record
and it is append-only.

**The last twenty captures, newest first:**

```sql
SELECT id,
       client_local_date,
       client_local_time,
       left(body, 60) AS preview,
       source,
       source_message_id
FROM thoughts
ORDER BY id DESC
LIMIT 20;
```

**One thought in full, with its attachments:**

```sql
SELECT t.id, t.body, t.client_created_at, t.client_timezone,
       a.source_filename, a.extracted_status, b.media_type, b.size_bytes, b.storage_key
FROM thoughts t
LEFT JOIN thought_attachments a ON a.thought_id = t.id
LEFT JOIN blobs b ON b.sha256 = a.blob_sha256
WHERE t.id = 1;
```

`extracted_status` is always `not_supported` in Release 1: attachments are
archived, never read. That is deliberate, not a gap.

**Did a redelivery create a duplicate?** It must not. This should always return
zero rows:

```sql
SELECT source, source_message_id, count(*)
FROM thoughts
GROUP BY source, source_message_id
HAVING count(*) > 1;
```

**Captures per day**, useful for seeing whether the habit is sticking:

```sql
SELECT client_local_date, count(*)
FROM thoughts
GROUP BY client_local_date
ORDER BY client_local_date DESC
LIMIT 14;
```

## Where acknowledgements land

Every committed thought writes a `thought.captured` row into `outbox_events` in
the same transaction. After a Discord capture commits, the bot replies directly
and marks the matching event delivered. If the reply or settlement fails, the
event remains pending. This is the table to inspect when a message was captured
but no reply appeared. Capture-acknowledgement retry is not yet wired, so those
pending events are diagnostic state rather than proof that an automatic retry
will run. API captures also leave acknowledgement events pending because there
is no Discord message to reply to and no consumer to route them elsewhere yet.

The bot now has a separate persistent consumer for `digest.ready` events. It
claims and retries only generated-digest delivery; it does not consume or send
capture acknowledgements.

**Anything still undelivered:**

```sql
SELECT id, event_type, aggregate_id, attempts, available_at, last_error
FROM outbox_events
WHERE delivered_at IS NULL
ORDER BY available_at;
```

**The acknowledgement for a specific thought:**

```sql
SELECT event_type, payload, attempts, delivered_at, last_error
FROM outbox_events
WHERE aggregate_id = '1';
```

The payload carries identifiers only — thought id, source, channel, attachment
count. Message text is never copied here, because this table is retried and
logged (docs/DESIGN.md 14.2).

## Where attachment files land

Files are stored by content hash under `TC_ATTACHMENT_ROOT`, fanned out two
levels:

```text
attachments/ab/cd/abcd...  ← the file, named by its own SHA-256
```

`blobs.storage_key` holds that relative path. Identical bytes are stored once
and referenced by every thought that carries them. Host-run processes default
to `./attachments`; Compose overrides the root to `/data/attachments` in its
named `attachments` volume.

**Verify a stored file matches its recorded hash:**

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yml exec api sha256sum /data/attachments/ab/cd/abcd...
```

## Who owns the workspace

```sql
SELECT w.id, w.name, w.timezone, w.digest_local_time,
       u.display_name, e.provider, e.external_user_id
FROM workspaces w
JOIN workspace_memberships m ON m.workspace_id = w.id
JOIN users u ON u.id = m.user_id
LEFT JOIN external_identities e ON e.user_id = u.id;
```

`external_user_id` is the Discord account allowed to capture. If it is missing
or wrong, messages are ignored without being stored (docs/DESIGN.md 4.1).

## Recovering from an embedding model conflict

**Symptom:** `embedding_sync.model_conflict` at **ERROR** in the `worker`
logs, one entry per affected document, carrying `event_id`, `document_id`,
`stored_model_id`, and `incoming_model_id`. The corresponding
`embedding.sync_requested` events keep failing and retrying with backoff,
eventually dead-lettering once they exhaust `outbox_events.max_attempts`
(`outbox.py`) — semantic sync for that workspace is effectively halted, not
silently degraded.

**Cause:** the embedding sidecar's model or pinned revision changed
(`TC_EMBEDDING_MODEL_ID` or `TC_EMBEDDING_MODEL_REVISION` in `.env`, followed
by a rebuild of the sidecar image - see below) without running a full
re-embed of the workspace first. These two variables are a **build-time**
pin, not a runtime one (review finding F16): `apps/embedding_sidecar/Dockerfile`
bakes the named model's weights into the image at `docker build` time and
then disables Hugging Face hub access at runtime (`HF_HUB_OFFLINE=1`), so
editing `.env` and only restarting the container changes nothing - the
running process still serves whatever was baked in. Changing the model for
real requires:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yml \
  --profile core up -d --build embedding-sidecar
```

before any of the recovery steps below, so the running sidecar actually
reports the new `incoming_model_id` its weights match. `document_embeddings` must
never hold vectors from two different models within one workspace
(docs/DESIGN.md 8.5, 9.2); the write path now refuses every write that would
violate that, rather than mixing models silently. This is a deliberate halt,
not a bug: embeddings are *derived* artifacts, so stopping only changes
*when* they get recomputed — nothing canonical is touched, and the raw
thoughts and attachments that produced them remain exactly as captured.

**Recovery** (audited, via the tracked `reembed` lifecycle —
`docs/adr/0010`'s addendum, round-4 finding F15; supersedes the earlier
hand-run `DELETE FROM document_embeddings` procedure this section used to
document):

1. Back up first — prevention is not recovery:

   ```bash
   ./scripts/backup.sh    # or scripts/backup.ps1 on PowerShell
   ```

2. Start (or resume) a tracked reembed sweep for the affected workspace:

   ```bash
   curl -X POST http://127.0.0.1:8080/v1/admin/reembed \
     -H "Authorization: Bearer $TC_API_BEARER_TOKEN"
   ```

   In one transaction, this records a `runs(kind='reembed', status='running')`
   row targeting whatever model the sidecar's `/health` currently reports,
   deletes every one of the workspace's `document_embeddings` rows, and
   re-enqueues one `embedding.sync_requested` event per current document —
   the same audited replacement for the old manual `DELETE` this endpoint
   exists to retire. A 503 response means the sidecar is unreachable (fix
   that first, per the F16 rebuild steps above, before retrying); a repeat
   call while a sweep is already `running` for the workspace is safe and
   just returns that same run.

3. The worker's `EmbeddingSyncLoop` delivers the re-enqueued events and
   reconciles the run's status (`succeeded`/`failed`) on its own poll cycle
   — no manual worker stop/restart is needed. Check progress with:

   ```sql
   SELECT status, embedding_model_id, started_at, finished_at, error_code
   FROM runs
   WHERE kind = 'reembed' AND workspace_id = '<workspace-id>'
   ORDER BY started_at DESC LIMIT 1;
   ```

   `status='failed'` with `error_code='embedding_sync_dead_lettered'` means
   at least one of this sweep's own sync events exhausted its retries —
   investigate the worker logs for that document before retrying the sweep.

## Running the checks

```bash
./scripts/check.ps1
```

Runs the lockfile check, formatter, linter, type checker, unit suite, and — when
Docker is reachable — the integration suite against a disposable database. A run
that could not execute a stage says `NOT RUN` and is not a passing run.

## Current scope and known gaps

The runnable stack captures allowlisted Discord text and attachments, exposes
authenticated API capture and reads, and preserves canonical data in
PostgreSQL. The API provides health, `/v1/thoughts`, `/v1/documents`,
`/v1/entities`, `/v1/search` (exact, semantic, and hybrid modes), `/v1/ask`,
`/v1/admin/khoj-sync`, `/v1/admin/embedding-sync`, and `/v1/admin/reembed`
routes. A separate
read-only `/debug` operator view exposes raw thoughts, entities, daily
digests, and journaled LLM runs; it is not the future unified UI. The Discord
bot also takes `/organize`, `/status`, `/search`, and `/ask` slash commands,
all restricted to the configured owner.

The `worker` processes closed capture windows, catches up missed windows after
startup, and writes versioned derived documents and entities; it also runs two
sync loops in parallel (ADR-0010 step 2, "running alongside Khoj sync, not
replacing it yet"): `tc_worker.khoj_sync_loop`, which pushes newly-written
documents into Khoj's index, and `tc_worker.embedding_sync_loop`, which
computes each newly-written document's current-revision embedding via the
first-party embedding sidecar and stores it in the `document_embeddings`
pgvector column. Both are outbox-driven and poll every 30s by default; an
operator can force an immediate backfill for either with
`POST /v1/admin/khoj-sync` or `POST /v1/admin/embedding-sync` respectively -
both only enqueue, delivery still happens on the relevant loop's next poll.
Neither semantic search nor Ask reads from `document_embeddings` yet (Slice 3
and Slice 4 of `docs/plans/khoj-retirement-completion.md` wire that up); this
loop only keeps the table current in the meantime. The Discord bot polls
queued daily-digest deliveries separately from capture acknowledgements.

The embedding sidecar's URL is two deliberately separate variables
(`env.example`): `TC_EMBEDDING_SIDECAR_BASE_URL` is the URL a *container*
uses (defaults to the in-container service name, and is forwarded into the
`worker` service by `deploy/compose/docker-compose.yml`, since only worker
constructs `HttpEmbeddingClient`); `TC_EMBEDDING_SIDECAR_HOST_BASE_URL` is the
URL the *host* uses once the contract-test overlay publishes the sidecar's
port to loopback, read only by `tests/contract/embedding_sidecar/conftest.py`
and both check scripts. Setting the wrong one has no effect on the other.
`upstage/solar-pro4`
(select) and `x-ai/grok-4.3` (organize) are reviewed and registered in safe
mode (`REVIEWED_MODELS`), but `TC_MODEL_ORGANIZE` and `TC_MODEL_SELECT` still
default to blank in `env.example`: an operator must opt in explicitly, or the
pipeline keeps running organize on the deterministic offline adapter.
Configuring a different provider model remains subject to ADR-0006's review
requirements.

**Ask needs explicit setup on top of the `ai` Compose profile actually
running (`docs/adr/0003`'s "Ask proxy" amendment):** it requires
`TC_ASK_ENABLED=true` (default `false`), `TC_ASK_PROVIDER_RETENTION_ACKNOWLEDGED=true`
(default `false`, an explicit owner acknowledgment that the configured chat
model's provider/retention policy has been reviewed), *and* Khoj's own chat
model configured (`TC_KHOJ_OPENAI_BASE_URL`/`TC_KHOJ_OPENAI_API_KEY`, blank by
default) - three independent opt-ins, none set by this project's own code,
before any question reaches a live model.

These are not equivalent failures. `TC_ASK_ENABLED` is this project's own
switch and is the only thing `enabled` reflects: leaving it at its default
makes `/ask`/`/v1/ask` report `enabled: false` explicitly, with no request to
Khoj at all. Leaving `TC_ASK_PROVIDER_RETENTION_ACKNOWLEDGED` at its default
while `TC_ASK_ENABLED=true` fails startup outright (a misconfiguration, not a
runtime state). Whether Khoj's *own* chat model is configured is not
something this project's code can check in advance - the API only learns
that when a chat call actually fails, at which point `TC_ASK_ENABLED=true`
still reports `enabled: true, degraded: true`, not `enabled: false`.

Local backup and restore validation ARE runnable (docs/incidents/0001-docker-
compose-down-deleted-the-real-dev-stack.md's residual-risk finding): `scripts/
backup.sh`/`.ps1` runs the `backup` Compose profile - `pg_dump` in custom
format at one consistent database snapshot, written atomically to a HOST
directory (`TC_BACKUP_ROOT`, never a Docker volume), plus an attachment
manifest/incremental copy and a `tc_app` grant catalog recorded from that same
snapshot. Both wrappers pin `-p thought-capture` explicitly and refuse to run
at all when an ambient `COMPOSE_PROJECT_NAME`, or one set in `.env`, names a
different project - a backup that silently captured a different, empty stack
would be worse than one that failed outright. Every artifact is created
restricted (`umask 077`, with each directory locked to 0700 before anything is
written into it) rather than tightened after the fact, so a full database dump
is never briefly world-readable on a host that enforces POSIX permissions.
Every artifact's filename is namespaced by a run id (a timestamp plus a
random `mktemp` suffix), not the timestamp alone, so two runs that are
serialized by the backup lock but happen to start in the same second never
collide on a filename - a collision would otherwise let a failing second run
silently overwrite a prior, successful run's dump under its own dump's name
while `latest.txt` kept pointing at a manifest that no longer matched what
was actually on disk.
`scripts/restore-test.sh`/`.ps1` then proves that backup actually
restores, against a throwaway, isolated Postgres under its own Compose project
- never `thought-capture` - checking schema/every foreign key (a
single-transaction `pg_restore`), every table's row count, every attachment's
blob hash, and that the restored `tc_app` role's grants match the backup-time
catalog and are actually usable (a live connection as `tc_app` can read
`thoughts` but is refused writing `users` or updating a thought's body).

**A green restore-test is not provenance.** Every integrity check it runs -
the dump's sha256, each attachment's blob hash, the row counts, the grant
catalog - compares the backup against values `backup.sh` recorded into the
same directory as the backup itself. That catches corruption at rest. It does
not catch tampering, because anything that could alter the dump could alter
the recorded checksum beside it. The mitigation today is network egress
containment, stated precisely rather than as a stronger guarantee it is
not: the restore-validation stack runs on an `internal: true` network with
no gateway, so a tampered dump cannot exfiltrate anything or reach further
hosts from that job. It does NOT sandbox local execution - `pg_restore`
connects as the scratch cluster's own superuser, and a custom-format dump is
a stream of SQL a superuser is permitted to run, so a hostile archive can
still execute commands inside the throwaway `postgres-scratch` container
(which has no state worth protecting and is discarded with the rest of the
throwaway project). Signed or authenticated backups (docs/DESIGN.md 12.2
names `age`, with the private key held off the backup destination) are
designed but not built, because they need a decided answer to where that key
lives - still an open owner decision in docs/DESIGN.md 19. **Until that
decision lands, restoring a backup that has
been off this host is gated on it, not on a green restore-test.**

**Recovery ordering matters.** The dump now carries `tc_app`'s privileges
(GRANT statements from migrations 0001-0008) rather than stripping them, so a
restore target must have the `tc_app` role provisioned - via
`deploy/compose/initdb/01-roles.sh`, the same script the real stack's own
`postgres` service runs at cluster creation - **before** `pg_restore` runs.
Bring Postgres up first (so its init scripts provision the role), then
restore; restoring into a cluster with no `tc_app` now fails loudly instead
of quietly producing a database whose application role has a login but zero
table privileges. That failure mode is intended, not a regression.

What is still NOT built, and remains docs/DESIGN.md 17's separately-gated
Phase 3 (Operational hardening): scheduling (nightly/quarterly cron), weekly
off-site replication, retention tiers, and encryption key custody - each has
its own still-open owner decision (docs/DESIGN.md 19: off-site provider and
retention tiers). The future unified custom UI is also not runnable yet.
