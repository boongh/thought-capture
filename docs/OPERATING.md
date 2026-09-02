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
PostgreSQL. The API provides health, `/v1/thoughts`, `/v1/documents`, and
`/v1/entities` routes. A separate read-only `/debug` operator view exposes raw
thoughts, entities, and daily digests; it is not the future unified UI.

The `worker` processes closed capture windows, catches up missed windows after
startup, and writes versioned derived documents and entities. The Discord bot
polls queued daily-digest deliveries separately from capture acknowledgements.
With no reviewed model registered, safe mode uses the deterministic offline
adapter; configuring a provider model remains subject to ADR-0006's review
requirements.

Khoj indexing/search/Ask, backup and restore jobs, retry delivery for capture
acknowledgements, and the future unified custom UI are not runnable yet. The
`ai` and `backup` Compose profiles described in the design are likewise not
defined yet.
