# Operating and inspection

How to run the local stack and see what the system has actually stored. Written
for the project owner; everything here is read-only unless stated otherwise.

## Starting the stack

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yml --profile core up -d
uv run alembic upgrade head
```

`--env-file .env` is required. Compose resolves a bare `.env` relative to the
compose file, not the repository root.

## Opening a database shell

```bash
docker exec -it thought-capture-postgres-1 psql -U tc_migrator -d thought_capture
```

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
the same transaction. The bot delivers it afterwards and sets `delivered_at`.
This is the table to look at when a message was captured but no reply appeared.

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

Files are stored by content hash under `TC_ATTACHMENT_ROOT` (default
`./attachments`), fanned out two levels:

```text
attachments/ab/cd/abcd...  ← the file, named by its own SHA-256
```

`blobs.storage_key` holds that relative path. Identical bytes are stored once
and referenced by every thought that carries them.

**Verify a stored file matches its recorded hash:**

```bash
sha256sum attachments/ab/cd/abcd...
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

## What does not exist yet

Slices 1–4 built storage. There is no Discord bot, HTTP API, organization
pipeline, or digest yet, so `thoughts` will be empty until Slice 5 wires the
Discord adapter. The tables and queries above are how you will observe it once
it does.
