# Thought Capture AI - System Design

- **Status:** Accepted implementation anchor
- **Version:** 1.9
- **Date:** 2026-09-08
- **Audience:** Small experienced engineering team
- **Owner:** Project owner

## 1. Executive decision

Build a self-hosted personal memory system whose critical path is Discord capture, not organization. Every accepted message is written immediately to an append-only canonical log. At 20:00 in the owner's configured timezone, a background pipeline organizes new captures into an immutable daily digest and versioned, evolving entity documents, then sends the digest back through Discord.

PostgreSQL owns canonical facts, provenance, revisions, deterministic filters, lexical search, and — since `docs/adr/0010` — semantic indexing via its `pgvector` extension. A small first-party embedding sidecar computes vectors locally; a first-party retrieval coordinator runs exact and semantic search as two SQL-adjacent paths against the same database and fuses them, and routes Ask through the project's own `OpenRouterProvider`. A later custom web UI will call only the coordinator's stable API and will present capture history, exact search, semantic search, Ask, revisions, and operations in one interface.

This design previously delegated semantic indexing, reranking, and Ask/RAG to a self-hosted third-party system, Khoj, integrated API-only and never forked (`docs/adr/0003`). `docs/adr/0010` retired that dependency: Khoj's lack of a stable custom-retriever contract and its own typed filters could not express the time/entity/source/document-revision filters this project needs any better than a first-party SQL query already does, its Ask path required a second, ungoverned LLM credential outside this project's own model-review discipline (`docs/adr/0006`), and its embedding step — always intended to run locally and cheaply (section 9.2) — needed no external system at all to keep running locally. Owning retrieval outright, rather than integrating a second system's opaque behavior, is now the accepted design; see `docs/adr/0010` for the full decision record and migration path.

### 1.1 Release 1 definition of done

Release 1 is complete only when all of the following are demonstrated on a clean local installation:

- An allowlisted Discord user sends an English text message; the system durably commits it and acknowledges it within five seconds at p95 under normal local conditions.
- Redelivery of the same Discord message creates no duplicate thought.
- File and image attachments are copied to content-addressed storage and linked to the raw thought. No OCR, vision inference, or transcription occurs.
- At 20:00 local time, the worker organizes captures received since the previous successful cutoff, sends a cited daily digest to Discord, and updates relevant evolving entity documents.
- `/organize` triggers the same idempotent pipeline manually and reports its run identifier and outcome.
- Every generated claim is traceable to one or more raw thought IDs.
- Re-running a capture window creates a new run and new revisions without destroying previous output.
- Search supports semantic similarity plus exact phrase, include/exclude terms, date range, time range, source, document type, and entity filters.
- Ask answers from indexed generated documents (self-hosted embedding search plus OpenRouter generation, `docs/adr/0010`) and displays validated references.
- Backup, export, restore, and a model-provider failure are exercised successfully.

## 2. Problem and product goal

Conventional note systems require classification at the moment of capture: title, folder, tags, filenames, and links. That tax is paid when attention is shortest, so many thoughts are never recorded. Retrieval tools cannot recover a corpus that was never created.

The product goal is to make capture nearly effortless while moving structure, entity resolution, summarization, and indexing to a recoverable background process. The system must remain useful if every derived artifact and AI provider disappears: the raw log and attachments are the durable personal record.

### 2.1 Success measures

| Measure | Release target | Measurement |
|---|---:|---|
| Capture acknowledgement latency | p95 < 5 seconds | timestamp from Discord event receipt to acknowledgement send |
| Capture durability | zero acknowledged losses | fault-injection integration tests |
| Duplicate handling | zero duplicate rows per Discord message | uniqueness constraint and replay test |
| Daily habit | non-zero capture on at least 20 of days 31-60 | local aggregate only |
| Known-item retrieval | recall@5 >= 0.80 | owner-maintained golden query set |
| Retrieval ranking | MRR >= 0.70 | same golden set |
| Grounding | 100% generated documents have sources; >= 0.98 sampled claim support | structural check plus eval sample |
| Pipeline cost | configurable alert at USD 5/month | persisted OpenRouter usage and price snapshots |
| Recovery | RPO <= 24 hours, RTO <= 2 hours locally | quarterly restore drill |

### 2.2 Non-goals for Release 1

- Multi-user authentication, collaboration, or sharing in production.
- A custom browser UI. (`docs/adr/0009` adds a minimal, loopback-only, read-only
  operator debug view - four plain HTML pages behind the existing bearer
  token - which this non-goal does not cover; it is not a step toward 4.4's
  future unified interface.)
- Voice capture or transcription.
- OCR, image understanding, attachment semantic indexing, or media generation.
- Automatic deletion or LLM rewriting of raw thoughts.
- Always-on screen/audio capture, wellbeing coaching, or social features.
- A public internet endpoint or mobile-native application.
- Cross-day automatic merging without an identified entity or project anchor.

## 3. Design principles

1. **Acknowledge only after durable commit.** Discord delivery is not evidence of storage.
2. **Raw means immutable.** Corrections are new events linked to old events; they are not updates.
3. **Derived means disposable but auditable.** Every derived version records run, model, prompt, and sources.
4. **Reversion is forward motion.** Restoring an older document creates a new revision whose parent is the current revision and whose content matches the selected historical revision.
5. **One stable application API.** Discord, the embedding sidecar, and the future UI are adapters around first-party use cases.
6. **Precise before impressive.** Deterministic filters and citations are requirements; chat polish is secondary.
7. **Provider isolation.** LLM, embeddings, Discord, and storage are ports with replaceable adapters.
8. **Single-user first, workspace-scoped always.** Every domain row carries workspace ownership so later isolation and shared brains do not require re-keying the database.
9. **Local by default.** Services bind to loopback and outbound disclosure is explicit.
10. **Version external systems.** Container images (embedding sidecar, PostgreSQL/pgvector), models, prompts, schemas, and migrations are pinned; upgrades are tested, not floated.

## 4. Scope and user experience

### 4.1 Discord capture

Release 1 supports either a direct message to the bot or messages in one configured private channel. The bot listens through the Discord Gateway and requires the `MESSAGE_CONTENT` privileged intent to receive message text and attachments outside mention-only cases. The allowlist contains the owner's Discord user ID and the configured guild/channel IDs. Messages from bots, webhooks, edits, and other users are ignored and audited without storing their content.

On a valid message:

1. Normalize Discord snowflake IDs, original Discord timestamp, content, and attachment metadata.
2. Stream each attachment to a temporary file, calculate SHA-256, verify the configured size and MIME policy, then atomically place it in content-addressed storage.
3. Insert the raw thought and attachment links in one database transaction.
4. Send a compact acknowledgement containing the thought ID. If commit fails, do not acknowledge; retry with bounded exponential backoff and alert locally.

Discord attachment URLs are signed and expire, so the worker must copy allowed attachments during ingestion. Attachments are archival only in Release 1. The absence of extracted content must be explicit in the API.

### 4.2 Daily cutoff and digest

The default cutoff is 20:00 `Asia/Bangkok`, configurable as an IANA timezone and local wall-clock time. A capture window is `(previous_successful_cutoff, current_cutoff]`, not a calendar day. This avoids losing late-evening thoughts and gives the owner one digest per operational day. Raw events still retain their true local calendar date and timezone for search.

If the machine is off at cutoff, a startup catch-up job processes every closed, unprocessed window oldest-first. A thought received after 20:00 appears in the next digest. A forced run may target the open window for testing and is labeled `provisional`; the scheduled run later supersedes its derived revisions without deleting it.

Discord receives:

- run status and capture-window times;
- a short digest designed to fit Discord message limits, split on section boundaries when needed;
- entity/project updates;
- source thought identifiers and a reference to the relevant document, resolvable through `/v1/documents` or Discord `/search`;
- a clear partial/failure notice instead of an invented result.

### 4.3 Search and Ask

The system exposes one retrieval API with three modes:

- `exact`: PostgreSQL date/time/entity/type/source filters plus phrase, prefix, full-text, and trigram matching;
- `semantic`: pgvector cosine-similarity search over each document's current-revision embedding, computed by the local embedding sidecar (`docs/adr/0010`);
- `hybrid`: both paths in parallel, normalized and fused with Reciprocal Rank Fusion (RRF).

Discord slash commands (`/search`, `/ask`) are the Release 1 human interface for semantic search and Ask — there is no separate third-party web UI in this design. The first-party `/v1/search` and `/v1/ask` contracts are the seam for a later unified UI.

### 4.4 Future unified interface

A custom UI is moderate work because it does not implement retrieval. It calls the gateway and renders a normalized model. Expected screens are Capture History, Daily Digests, Entities, Search, Ask, Revision History/Diff, Runs, and Settings. The gateway owns auth, query parsing, citations, pagination, and streaming; the browser never contacts PostgreSQL, OpenRouter, or the embedding sidecar directly.

Retrieval (exact and semantic search, Ask) is first-party code behind the gateway, not a third-party UI to fork or embed. If a future embedding-model or provider swap changes behavior, isolate it in the sidecar's own contract tests. A future dedicated reranking or retrieval-quality improvement is welcome, but no core feature depends on it landing (`docs/adr/0010` §1).

## 5. Architecture

```mermaid
flowchart LR
    D[Discord user] -->|Gateway events| B[Discord bot]
    B -->|commands| A[Application API/use cases]
    A --> P[(Canonical PostgreSQL + pgvector)]
    B --> O[(Attachment store)]
    W[Worker/scheduler] --> P
    W --> O
    W -->|structured completions| R[OpenRouter]
    W -->|embedding requests| E[Embedding sidecar]
    E -->|vector| P
    U[Discord /search, /ask now; custom UI later] --> G[Retrieval gateway]
    G -->|exact + semantic filters| P
    G -->|Ask evidence + generation| R
    G -->|normalized cited results| U
    W -->|digest| D
```

### 5.1 Deployable processes

| Process | Responsibility | State |
|---|---|---|
| `api` | REST gateway, health, run commands, raw/document reads, normalized search/Ask | stateless |
| `discord-bot` | Gateway connection, capture adapter, acknowledgement, slash commands, digest delivery | stateless except Discord connection |
| `worker` | persistent schedules, organization, export, embedding sync, retries, digest dispatch | leases and run rows in PostgreSQL |
| `postgres` | canonical log, derived revisions, provenance, entities, exact search, schedules, semantic index (`pgvector`, `docs/adr/0010`) | canonical state |
| `embedding-sidecar` | local embedding computation for search/Ask (`docs/adr/0010`) | stateless |
| `attachments` volume | content-addressed original files | canonical binary state |

There is a single PostgreSQL database; the embedding sidecar is a stateless compute service with no database of its own to isolate — unlike the retired Khoj-era topology, there is no second schema or server to keep separate. A VPS deployment may still split PostgreSQL onto its own host without application changes.

### 5.2 Language and framework allocation

- **Python 3.14.x:** all first-party services and libraries except the embedding sidecar (below). Pin the patch version in `.python-version` and container base image; upgrade through CI.
- **FastAPI:** HTTP transport and OpenAPI. Business rules remain framework-free.
- **Pydantic v2:** boundary validation and LLM structured-output schemas.
- **SQLAlchemy 2 async + psycopg 3:** persistence. Use explicit repository methods; no database calls from Discord event handlers outside application use cases.
- **Alembic:** first-party schema migrations, executed as a deployment step.
- **discord.py:** Discord Gateway, commands, attachments, and responses.
- **APScheduler 3.x initially:** scheduling in the worker with a PostgreSQL-backed lease. Replace with a queue only when concurrency or load justifies it.
- **pytest, Hypothesis, Ruff, mypy:** verification and quality gates.
- **Markdown:** portable generated-document interchange format. Embeddings are computed directly from stored `body_markdown` (section 8.4); there is no separate export/index-file step.

The embedding sidecar (`docs/adr/0010`) is pinned as its own first-party OCI image, built for Python 3.12 (`sentence-transformers`/`torch` do not yet ship Python 3.14 wheels); it must not share the first-party Python 3.14 environment.

### 5.3 Dependency direction

`domain` has no framework imports. `application` depends on domain ports. `infrastructure` implements PostgreSQL, Discord, object storage, OpenRouter, and embedding-sidecar adapters. `apps/*` wires dependencies. This hexagonal boundary is what makes Discord replaceable and permits local models later.

## 6. Canonical data model

Every table except global migration metadata carries `workspace_id`. Release 1 seeds one owner, one workspace, and one membership. This adds negligible cost now and supports two later modes: isolated personal workspaces and multi-member shared workspaces.

### 6.1 Identity and workspace

```sql
CREATE TABLE users (
  id uuid PRIMARY KEY,
  display_name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE workspaces (
  id uuid PRIMARY KEY,
  name text NOT NULL,
  mode text NOT NULL CHECK (mode IN ('personal','shared')),
  timezone text NOT NULL,
  digest_local_time time NOT NULL DEFAULT '20:00',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE workspace_memberships (
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  user_id uuid NOT NULL REFERENCES users(id),
  role text NOT NULL CHECK (role IN ('owner','editor','viewer')),
  PRIMARY KEY (workspace_id, user_id)
);

CREATE TABLE external_identities (
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  user_id uuid NOT NULL REFERENCES users(id),
  provider text NOT NULL,
  external_user_id text NOT NULL,
  PRIMARY KEY (provider, external_user_id),
  UNIQUE (workspace_id, user_id, provider)
);
```

### 6.2 Append-only capture layer

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE thoughts (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  author_user_id uuid NOT NULL REFERENCES users(id),
  source text NOT NULL CHECK (source IN ('discord','api','import')),
  source_message_id text NOT NULL,
  source_channel_id text,
  body text NOT NULL DEFAULT '',
  client_created_at timestamptz NOT NULL,
  client_timezone text NOT NULL,
  client_local_date date NOT NULL,
  client_local_time time NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  content_language text NOT NULL DEFAULT 'en',
  correction_of bigint REFERENCES thoughts(id),
  body_tsv tsvector GENERATED ALWAYS AS
    (to_tsvector('english', coalesce(body,''))) STORED,
  UNIQUE (source, source_message_id)
);

CREATE INDEX thoughts_workspace_time_idx
  ON thoughts (workspace_id, client_created_at DESC);
CREATE INDEX thoughts_tsv_idx ON thoughts USING gin (body_tsv);
CREATE INDEX thoughts_trgm_idx ON thoughts USING gin (body gin_trgm_ops);

CREATE TABLE blobs (
  sha256 char(64) PRIMARY KEY,
  size_bytes bigint NOT NULL,
  media_type text NOT NULL,
  storage_key text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE thought_attachments (
  thought_id bigint NOT NULL REFERENCES thoughts(id),
  blob_sha256 char(64) NOT NULL REFERENCES blobs(sha256),
  source_filename text NOT NULL,
  source_url_expires_at timestamptz,
  extracted_status text NOT NULL DEFAULT 'not_supported',
  PRIMARY KEY (thought_id, blob_sha256, source_filename)
);
```

Database roles deny `UPDATE` and `DELETE` on `thoughts`; an additional trigger rejects mutation except in a restore-only administrative session. Imports and corrections append rows.

### 6.3 Runs, documents, and reversible revisions

```sql
CREATE TABLE runs (
  id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  kind text NOT NULL CHECK (kind IN
    ('organize','force_organize','khoj_sync','embedding_sync','export','restore_test','reembed','ask')),
  window_start timestamptz,
  window_end timestamptz,
  status text NOT NULL CHECK (status IN ('queued','running','succeeded','partial','failed')),
  prompt_version text,
  model_provider text,
  model_id text,
  embedding_model_id text,
  input_tokens bigint,
  output_tokens bigint,
  estimated_cost_usd numeric(12,6),
  started_at timestamptz,
  finished_at timestamptz,
  error_code text,
  error_detail text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE documents (
  id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  kind text NOT NULL CHECK (kind IN
    ('daily_digest','person','project','place','organization','topic','decision','todo')),
  stable_key text NOT NULL,
  title text NOT NULL,
  current_revision_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, kind, stable_key)
);

CREATE TABLE document_revisions (
  id uuid PRIMARY KEY,
  document_id uuid NOT NULL REFERENCES documents(id),
  parent_revision_id uuid REFERENCES document_revisions(id),
  run_id uuid NOT NULL REFERENCES runs(id),
  revision_number integer NOT NULL,
  body_markdown text NOT NULL,
  body_sha256 char(64) NOT NULL,
  change_summary text NOT NULL,
  change_kind text NOT NULL CHECK (change_kind IN
    ('create','organize','manual_restore','supersede')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, revision_number),
  UNIQUE (id, document_id)
);

-- (document_revisions.id, document_revisions.document_id) is already
-- unique (id alone is the primary key), which is what lets the
-- composite foreign keys below pin *both* "this is a real revision" and
-- "it belongs to this exact document" in one constraint, instead of two
-- independently-satisfiable single-column foreign keys that could each
-- individually validate while disagreeing with each other about which
-- document they jointly describe (docs/adr/0010's "document_embeddings"
-- fix below applies the identical pattern; this tightens an existing gap
-- in documents.current_revision_id noticed while designing that fix).

ALTER TABLE documents ADD CONSTRAINT documents_current_revision_fk
  FOREIGN KEY (current_revision_id, id) REFERENCES document_revisions (id, document_id);

CREATE TABLE revision_sources (
  revision_id uuid NOT NULL REFERENCES document_revisions(id),
  thought_id bigint NOT NULL REFERENCES thoughts(id),
  support_type text NOT NULL CHECK (support_type IN ('direct','context')),
  PRIMARY KEY (revision_id, thought_id)
);
```

Store full Markdown snapshots for reliable recovery. Produce unified diffs on read or cache them as non-canonical artifacts. A restore selects a historical snapshot and writes it as the next revision. Daily digest `stable_key` is the capture-window end timestamp; entity documents use normalized entity IDs.

**`runs.kind` gains `'embedding_sync'` without removing `'khoj_sync'`.** `docs/adr/0010`'s migration plan requires the embedding sidecar to run alongside Khoj sync before cutover (its "Migration and rollback" step 2), and any already-deployed instance's migration history created the `runs.kind` CHECK constraint with `'khoj_sync'` in it. Replacing that value outright — narrowing the CHECK constraint in the same migration that adds the new one — either fails on a database holding a `'khoj_sync'` row or forces relabeling historical rows as something they were not, neither of which is compatible with this project's append-only, non-destructive migration discipline (section 3, principle 2). This ADR's Slice 1 migration is therefore purely additive: it widens the CHECK to `('organize','force_organize','khoj_sync','embedding_sync','export','restore_test','reembed')`. `'khoj_sync'` is dropped from the CHECK only in the later, evidence-gated Khoj-removal migration (`docs/adr/0010`'s "Migration and rollback" step 5), the same migration that removes the Khoj container/adapter/credentials — never before cutover is proven. (In practice neither value has ever been written by any code path — `docs/adr/0003`'s "Index sync run-tracking scope" left `'khoj_sync'` reserved-but-unused because index sync is fully decoupled from `runs`, and this design keeps that same decoupled shape for `'embedding_sync'`, section 7.2 step 11 — but the CHECK constraint's own migration safety does not depend on whether a value happens to be in use.)

### 6.4 Entities and mentions

```sql
CREATE TABLE entities (
  id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES workspaces(id),
  entity_type text NOT NULL CHECK (entity_type IN
    ('person','project','place','organization','topic')),
  canonical_name text NOT NULL,
  normalized_name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, entity_type, normalized_name)
);

CREATE TABLE entity_aliases (
  entity_id uuid NOT NULL REFERENCES entities(id),
  alias text NOT NULL,
  normalized_alias text NOT NULL,
  PRIMARY KEY (entity_id, normalized_alias)
);

CREATE TABLE entity_mentions (
  entity_id uuid NOT NULL REFERENCES entities(id),
  thought_id bigint REFERENCES thoughts(id),
  revision_id uuid REFERENCES document_revisions(id),
  run_id uuid NOT NULL REFERENCES runs(id),
  surface_form text NOT NULL,
  confidence numeric(4,3) NOT NULL,
  CHECK ((thought_id IS NOT NULL) <> (revision_id IS NOT NULL))
);
```

Automatic alias merges are forbidden below a configurable confidence threshold. Ambiguous candidates remain separate and appear in a review queue. Release 1 may expose that queue through API only.

### 6.5 Outbox, schedules, and index state

Use a transactional outbox so a database commit and Discord acknowledgement/digest intent cannot diverge. `outbox_events` contains event type, aggregate ID, JSON payload, attempt count, lease, next attempt, and delivery timestamp. Workers use `FOR UPDATE SKIP LOCKED`.

`capture_windows` records cutoff boundaries and the successful organize run. `document_embeddings` (section 8.4, `docs/adr/0010`) holds one row per document — keyed by `document_id`, not `revision_id` — carrying whichever revision's vector was most recently embedded successfully, its `embedding_model_id`, and `updated_at`; a failed sync attempt stays on the triggering outbox event's own `attempts`/`last_error` rather than this table, since a document's very first attempt could fail before any embedding exists at all. A revision becomes searchable only once `document_embeddings.revision_id` matches `documents.current_revision_id` — every semantic-search query enforces this equality directly rather than trusting the sync loop to always be caught up (section 8.4's currentness guarantee). `run_context_selections` (section 7.3.5) records per-run organize context assembly and is diagnostic, not canonical.

## 7. Core workflows

### 7.1 Capture sequence

```mermaid
sequenceDiagram
    participant U as Discord user
    participant B as Discord bot
    participant S as Capture use case
    participant DB as PostgreSQL
    participant FS as Attachment store
    U->>B: message + optional files
    B->>S: normalized capture command
    S->>FS: stream, validate, hash, atomic store
    S->>DB: transaction: thought + blob links + outbox
    DB-->>S: thought ID / existing ID on conflict
    S-->>B: durable result
    B-->>U: acknowledgement
```

If blob storage succeeds and the database transaction fails, a garbage-collection job removes unreferenced blobs after a safety interval. If attachment copy fails, the thought is not acknowledged; Release 1 treats the message as one atomic capture.

### 7.2 Organization pipeline

1. Acquire a workspace/window advisory lock.
2. Create a run row and load raw thoughts in stable timestamp/ID order.
3. Normalize Discord markup while preserving original body.
4. Assemble model context under the contract in section 7.3: the complete entity index plus selected entity-document bodies.
5. Detect candidate entities using structured model output plus existing aliases.
6. Cluster thoughts using time proximity, shared entities, and semantic hints. Release 1 may use the LLM for clustering; its output is validated and source-complete.
7. Generate the daily digest and proposed updates for touched entity documents only, as Pydantic-validated JSON. Untouched documents are not regenerated.
8. Verify every referenced thought belongs to the window/workspace and every input thought is either covered or explicitly classified `unorganized`. Record context recall for the run.
9. Write full document revisions and provenance in one transaction. Never write a partial document set after validation failure.
10. Export changed revisions to deterministic Markdown files.
11. Embed changed documents' current revisions and upsert their `document_embeddings` row, decoupled from this run entirely (`docs/adr/0010`, replacing ADR-0003's Khoj-era "Index sync run-tracking scope" addendum with the same decoupled-outbox shape): one `embedding.sync_requested` outbox event per document, delivered by an independent worker poller (`EmbeddingSyncLoop`). Canonical revisions remain valid and sync retries independently; failure never touches this run's own status.
12. Enqueue the Discord digest and mark success only after canonical commit. Discord delivery can retry without rerunning the LLM.

### 7.3 Context assembly for organization

The organize prompt must carry enough context to extend existing entity documents correctly without carrying the whole corpus. Context assembly is therefore a first-class pipeline stage with its own contract, its own invariants, and its own failure metric. It is not prompt plumbing.

Two properties of this workload set the design. First, the entity *index* is cheap and the entity *bodies* are expensive: a name, type, alias list, one-line summary, and last-mentioned date cost roughly 40 tokens per document, while a body costs 500-1500 and grows monotonically. Second, prompt caching is not a cost lever at this cadence. The longest available cache lifetime is one hour and the scheduled organize run fires once per day, so a cached corpus prefix has always expired before the next run and would incur the cache-write premium every time. Selection, not caching, is what keeps this stage affordable.

The two failure modes are asymmetric and must be treated differently:

- **Fragmentation** - the model creates a second document for an entity that already exists because it did not know the entity existed. This corrupts the corpus silently and compounds across runs. It is prevented outright by always supplying the complete index.
- **Thin detail** - the model updates a document without its full prior body. This is visible in the digest, bounded to one revision, and correctable on a later run.

Assembly buys out the first failure for a fixed, small token cost and then optimizes the second.

#### 7.3.1 Tier 1 - the complete entity index

Every organize prompt contains one index row for every entity document in the workspace, with no selection applied. A row contains the document `stable_key`, entity type, canonical name, known aliases, the document's bounded `Summary` section, the last-mentioned date, and the open-thread count.

The index is what makes entity reuse possible, so it is never truncated, sampled, or filtered. If index size ever becomes material, the response is to shorten each row, not to drop rows.

#### 7.3.2 Tier 2 - selected document bodies

Bodies are selected in two passes. The first is deterministic and free; the second is a single cheap model call.

| Signal | Source | Purpose |
|---|---|---|
| `alias` | `entities.normalized_name` and `entity_aliases`, exact and trigram matched against window text | Direct naming, including misspellings |
| `recency` | `entity_mentions` over the last `context.recency_windows` windows | Unnamed continuations of recent subjects |
| `cooccurrence` | Historical co-mention frequency with already-selected entities, above `context.cooccurrence_threshold` | Structural expansion without embeddings |
| `open_thread` | Documents of kind `todo` or `decision` with unresolved items | Standing context that is valuable whether or not it is mentioned |
| `embedding` | Cosine similarity between the window text and locally computed entity-document embeddings, top `context.embedding_top_k` | Topical relevance where the entity is neither named, recent, nor co-occurring (second wave; see 7.3.6) |
| `selector` | One `select` model call over the window text plus the Tier 1 index, returning `stable_key` values | Referential cases signals cannot resolve: pronouns, "the thing we discussed", implied projects |

The `select` call is a single request with a small structured output, not an agentic loop. It resolves most of what a tool-calling retriever would find while re-sending context once instead of once per turn, and because it is one deterministic-shaped call it stays cacheable by prompt hash for development and reproducible for evaluation.

#### 7.3.3 Generated document format

Generated entity documents use a fixed section order so that partial inclusion is well defined:

```markdown
## Summary          # bounded; source of the Tier 1 index line
## Current state    # bounded
## Open threads     # bounded
## Timeline         # append-oriented; truncatable from the tail
```

Inclusion is graded rather than binary:

| Mode | Content sent | Applies to |
|---|---|---|
| `index_only` | Tier 1 row only | Every unselected document |
| `partial` | Summary, Current state, Open threads, last `context.timeline_tail_entries` timeline entries | Marginal selections and documents over `context.max_body_tokens` |
| `full` | Entire body | High-confidence selections within budget |

Document format is a retrieval concern, not only a readability concern. Changing this section contract after the corpus exists requires regenerating documents, so it is fixed before the first organize prompt ships.

#### 7.3.4 Invariants

1. The entity index is complete. Every entity document in the workspace appears in every organize prompt.
2. Selection is additive. The final set is the union of the deterministic signals and the selector output; the selector can add documents but can never remove one that a deterministic signal produced.
3. Selector failure is non-fatal. On timeout, invalid output, or provider error, assembly proceeds with the deterministic set and the run records the degradation.
4. Expansion is bounded. If generation or validation reveals a required document that was not loaded, generation may be retried exactly once with the expanded set. There is no loop.
5. Only touched documents are regenerated. Full-snapshot revisions do not require rewriting untouched documents; those keep their current revision and no new revision row is written.
6. Assembly is recorded. Every run persists what was considered, what was selected, by which signal, and at which inclusion mode.

Invariant 2 is what makes a cheap model safe in this position: a poor selector call costs tokens, never correctness.

#### 7.3.5 Observability and context recall

```sql
CREATE TABLE run_context_selections (
  run_id uuid NOT NULL REFERENCES runs(id),
  document_id uuid NOT NULL REFERENCES documents(id),
  signals text[] NOT NULL,
  inclusion text NOT NULL CHECK (inclusion IN ('index_only','partial','full')),
  body_tokens integer NOT NULL DEFAULT 0,
  referenced_in_output boolean NOT NULL DEFAULT false,
  PRIMARY KEY (run_id, document_id)
);
```

After generation, every document referenced by the model output is marked `referenced_in_output`. This defines the stage's own quality metric:

```text
context_recall = referenced documents included as partial or full
                 / referenced documents
```

A document that was referenced while `index_only` is a retrieval miss. Without this metric an incorrect digest cannot be attributed: a selection failure and a generation failure look identical from the output alone. Target `context_recall >= 0.95`, alerting below.

#### 7.3.6 Cost envelope

| Component | Per-run input budget |
|---|---:|
| Entity index, all documents | ~2k tokens |
| Selected bodies | <= 8k tokens |
| Window thoughts | ~2k tokens |
| Instructions and schema | ~1k tokens |
| **Total input** | **<= 13k tokens** |

Once assembly is bounded this way, run cost is dominated by *output* tokens - the bodies actually rewritten - which is why invariant 5 is the largest single cost lever in the pipeline. Assembly configuration (`context.max_body_tokens`, `context.max_selected_documents`, `context.timeline_tail_entries`, `context.recency_windows`, `context.cooccurrence_threshold`, `context.embedding_enabled`, `context.embedding_top_k`) is workspace-scoped and versioned with the prompt.

Embedding selection is a planned second-wave signal, not a deferral. Entity-document embeddings are computed from each revision's `Summary` and `Current state` sections at revision-write time, inside the transaction that writes the revision, so they are fresh by construction. This is distinct from the pgvector-backed document semantic index (section 8, `docs/adr/0010`), which covers generated documents and is by construction one run stale at organize time; organize must not depend on it, and depending on it would invert the Phase 1 / Phase 2 order.

The embedding model runs locally rather than through OpenRouter. Text embedding is inexpensive to host, and keeping it local removes per-run cost, removes a network round trip from the organize path, and avoids disclosing document content to a second provider (section 12.1). At Release 1 corpus size the vectors are stored as `float4[]` alongside the revision and compared by direct cosine similarity; pgvector is unnecessary until a linear scan is measurably expensive, and the pinned `postgres` image does not carry the extension. Changing the embedding model requires a `reembed` run and is recorded in `runs.embedding_model_id`.

Sequencing is driven by measurability, not effort. The deterministic signals and the selector call ship first so that `context_recall` has a baseline; the embedding signal is then enabled behind `context.embedding_enabled` and judged by whether that number moves. Enabling every signal at once makes an underperforming or redundant signal impossible to identify.

### 7.4 Structured output contract

```python
class ProposedDocument(BaseModel):
    stable_key: str
    kind: Literal[
        "daily_digest", "person", "project", "place",
        "organization", "topic", "decision", "todo"
    ]
    title: str = Field(min_length=1, max_length=120)
    body_markdown: str
    source_thought_ids: list[int] = Field(min_length=1)
    mentioned_entities: list[EntityMention]
    change_summary: str = Field(max_length=300)
    confidence: float = Field(ge=0, le=1)

class OrganizationResult(BaseModel):
    documents: list[ProposedDocument]
    unorganized_thought_ids: list[int]
```

The prompt forbids facts absent from sources, requires first-person voice where appropriate, resolves relative dates using each thought's timestamp/timezone, and never treats attachments as understood content in Release 1. One repair attempt may include validation errors; a second failure aborts the stage.

### 7.5 Search sequence

The request may provide structured filters directly. If it provides only natural language, the coordinator may ask a cheap model to produce a validated `QueryPlan`; the original query is always retained. (`docs/adr/0010` retires Khoj as the semantic channel; this section describes the self-hosted replacement.)

1. PostgreSQL applies workspace, date/time, entity, type, source, and lexical constraints.
2. The coordinator calls `EmbeddingPort.embed` (the local embedding sidecar, `docs/adr/0010`) on the query text, then runs one SQL query against the workspace-scoped pgvector column, joined with the same date/entity/type/source filters as the exact path in the same `WHERE` clause — not a second system's filter language re-expressed and hoped to be honored. This query is an exact scan in v1, not an approximate-index-accelerated one, specifically so a selective filter cannot silently under-return matches (8.4 explains why and states the trigger for revisiting).
3. Both paths return stable document/revision/source identifiers directly from this project's own schema.
4. Normalize ranks and fuse with RRF using `k=60`; exact phrase matches receive a documented deterministic boost before rank assignment.
5. Deduplicate by revision ID and return evidence snippets, scores by channel, and citations. Because each document's current revision has exactly one embedding row (`docs/adr/0010` §3, no chunking), a duplicate-hit-by-filename class of bug is structurally impossible here, not merely handled.

If the embedding sidecar is unavailable, hybrid search degrades to exact search with `degraded=true`; it never returns an empty success that implies no memory exists.

### 7.6 Ask sequence

Ask assembles evidence through the same `EmbeddingSearchPort` search described in 7.5 (with query filters applied directly in SQL, not appended to a second system's query string), then generates an answer over that evidence through `OpenRouterProvider` using the reviewed `TC_MODEL_ASK` slug (`docs/adr/0006`'s safe-mode/ZDR controls apply exactly as they do to `organize`/`select`/`query_plan`). Strict filters are always honored, because they are ordinary SQL predicates on the same query that produces the semantic channel — there is no second system whose filter-expressiveness must first be probed, so the Khoj-era "capability code" fallback for an unsupported filter no longer applies to the semantic channel; a request only degrades if the embedding sidecar or `TC_MODEL_ASK` itself is unavailable.

**Evidence packet.** `EmbeddingSearchPort.search`'s top-N hits (bounded by `context.ask_evidence_top_k`, a new workspace-scoped config value alongside the existing `context.*` settings in 7.3.6) are assembled into a numbered evidence list before any model call:

```python
class AskEvidenceItem(BaseModel):
    index: int              # 1-based, stable for this request only
    document_id: UUID
    revision_id: UUID
    title: str
    snippet: str            # bounded excerpt of body_markdown, not the full body
```

**Empty-evidence behavior.** If the evidence list is empty, `TC_MODEL_ASK` is never called. Ask returns a fixed, non-generated response (`AskAnswer.answer = "No indexed documents matched this question."`, `evidence: []`) — the same "never invent a result" discipline principle 6 and `docs/DESIGN.md` 1's grounding requirement already apply everywhere else. This differs from Khoj's own behavior, which called its configured chat model even with nothing indexed and relied on the model itself producing a canned no-notes reply (ADR-0003's "Ask proxy" amendment); this design makes the same outcome structural rather than dependent on model behavior.

**Prompt.** The prompt instructs the model to answer only from the numbered evidence items and to mark every claim drawn from evidence with an inline bracketed citation matching that item's `index` (for example `[2]`), the same "quoted data, not instructions" framing principle 12.2 already requires for organize prompts.

**Streaming provider contract — new, not reused.** `organize`/`select`/`query_plan` never stream: `LLMProvider.complete()` (`packages/domain/src/tc_domain/llm.py`) returns one `LLMResponse` after `OpenRouterProvider.complete()` (`packages/infrastructure/src/tc_infrastructure/llm/openrouter.py`) calls `chat.completions.create()` without `stream=True`. Ask needs incremental output, so `LLMProvider` gains a second method, not a rewrite of `complete()`:

```python
@dataclass(frozen=True, slots=True)
class LLMStreamChunk:
    delta: str                        # "" for a chunk carrying only metadata
    finish_reason: str | None = None  # non-None only on the terminal chunk
    model_served: str | None = None   # populated once known, from the first chunk on
    generation_id: str | None = None
    input_tokens: int | None = None   # populated only on the terminal chunk
    output_tokens: int | None = None
    cost_usd: Decimal | None = None


class LLMStreamInterrupted(LLMError):
    """The stream failed after at least one chunk already carried content.

    Carries `partial_text` (every `delta` yielded so far, concatenated) and
    whatever usage fields were known, so the caller can still use - and
    journal - the content that already arrived rather than losing it.
    """


class LLMProvider(Protocol):
    ...
    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        """Issue one call, yielding incremental content chunks."""
        ...
```

`OpenRouterProvider.stream()` sends the same `messages`/`temperature`/`max_tokens` and the same `provider_routing` policy dict `complete()` already builds (`allow_fallbacks`, `data_collection: deny`, `zdr: true`, `provider.only` — `require_parameters`/`response_format` do not apply, since Ask's output is free-text prose, not schema-validated JSON), plus `stream: True` and `stream_options: {"include_usage": true}` so OpenRouter's own terminal SSE event carries usage the same way the non-streaming response's `usage` field does today.

**Policy enforcement holds mid-stream, not only at the end.** The served-model check (`complete()`'s existing `served not in self._allowed_served_models` guard) runs against the **first** chunk's `model` field, before any `delta` is yielded to the caller — an unapproved substituted model is rejected with `LLMError` before a single token of its output reaches the user, not after the fact.

**Errors after partial output are distinguishable from errors before any output.** A transport/timeout/status failure before the first content-bearing chunk raises `LLMError`, identically to `complete()` today. The same failure after at least one non-empty `delta` was already yielded raises `LLMStreamInterrupted` instead, carrying the partial text accumulated so far. Ask's caller (`AskQuestion`) treats `LLMStreamInterrupted` as the mid-stream degrade case: `degraded: true`, the evidence list still attached (as already stated below), and the partial text is discarded rather than shown as if it were a complete, citation-validated answer — an interrupted stream has not gone through citation validation and must not be presented as a finished one.

**Usage journaling needs a run to attach to, and `/v1/ask` has none by default.** `llm_calls.run_id` (`docs/adr/0008`, `migrations/versions/0004_derived_documents_and_provenance.py`) is `NOT NULL REFERENCES runs(id)` — every existing caller (`organize`/`select`/`query_plan`) already has a `runs` row because a pipeline run is what triggers them. An on-demand `/v1/ask` call has no such row. Two options were considered: give Ask its own workspace-scoped journal table decoupled from `runs` entirely, or give each Ask call a dedicated, lightweight `runs` row of a new kind. **This design chooses the latter** — a dedicated per-request `ask` run — because a second journal table would duplicate most of `llm_calls`' own columns (prompt/schema version, request/response, tokens, cost, latency, error) for no reason beyond avoiding the `runs` table, the same "don't invent a second place to track this" reasoning `docs/adr/0010` §3 already applied when reusing `runs.embedding_model_id` instead of a new column elsewhere. Reusing `runs` also means Ask calls are visible through `GET /v1/runs/{id}` for free, roll into the existing OpenRouter cost/budget tracking (`runs.estimated_cost_usd`, section 2.1's "Pipeline cost" measure) without a second cost-tracking path, and are covered by the existing backup/export machinery (14.3) with no new code.

Concretely:

- `runs.kind` gains `'ask'` (schema above). `llm_calls.step` gains `'ask'` (`migrations/versions/0004_derived_documents_and_provenance.py`'s CHECK constraint). `LLMStep` (`packages/domain/src/tc_domain/llm.py`) gains `ASK = "ask"`.
- **No run is created at all when evidence is empty** — the fixed, non-generated "no indexed documents" response (above) involves no model call, so there is nothing to journal. A `runs` row means "a model call was attempted," not "an Ask request was received."
- When evidence is non-empty, `AskQuestion` inserts one `runs` row (`kind='ask'`, `status='running'`, `workspace_id` from auth context, `model_id=TC_MODEL_ASK`, `prompt_version=<ask prompt's version>`, `window_start`/`window_end` left `NULL` — an ask run has no capture window, matching `export`/`restore_test`/`reembed`'s existing use of the same nullable columns) **before** calling `LLMProvider.stream()`, mirroring organize's own step 2 ("create a run row" before doing any model work) — an attempted call is durably recorded even if it then fails.
- On stream success (a terminal chunk with `finish_reason` reached): one `llm_calls` row is written (`run_id`=the ask run, `step='ask'`, `sequence=1`, full accumulated content in `response_raw`, `model_served`/`generation_id`/`input_tokens`/`output_tokens`/`estimated_cost_usd` from the terminal `LLMStreamChunk`), and the run is updated to `status='succeeded'`, `finished_at=now()`, with `model_provider`/`input_tokens`/`output_tokens`/`estimated_cost_usd` copied up from that same call. Citation validation (below) then runs on the accumulated text — `citations_unverified` is a property of the *answer's grounding quality*, tracked on the returned `AskAnswer`, and never changes the run's `status`: a call that completed cleanly but produced zero valid citations is still a `succeeded` run with an unverified answer, not a failed one.
- On `LLMError` before any content: one `llm_calls` row is written with `error_code` set and `response_raw`/`input_tokens`/`output_tokens` left `NULL` (matching how `error_code`'s existing nullable-response shape already accommodates a failed organize call), and the run is updated to `status='failed'`.
- On `LLMStreamInterrupted` (content already streamed, then cut short): one `llm_calls` row is written with `response_raw` holding the partial accumulated text, `error_code` set, and whatever usage fields the interruption made available (often `NULL`), and the run is updated to `status='partial'` — "some output was produced but the call did not finish cleanly," distinct from `'failed'`'s "nothing usable was produced," and distinct from `'succeeded'`'s "the call completed." The `AskAnswer` returned to the caller still reports `degraded: true` per the Degradation note below; the partial text is not shown as if it were a finished answer even though it is durably journaled.
- **Retention and export follow automatically.** Ask runs and their `llm_calls` rows live in the same tables as every other run, so section 14.3's nightly `pg_dump` and monthly portable export already cover them, and section 12.2's "never log raw bodies/prompts" and "`llm_calls` is as sensitive as `thoughts`" requirements (`docs/adr/0008`) apply unchanged — no new backup, export, or redaction code path is needed.
- Discord `/ask` calls the same `AskQuestion` use case as `/v1/ask`, so both surfaces share this exact run/journal mechanism — there is no separate, divergent journaling path for the bot to accidentally get wrong.

**Tests required before Slice 4 can rely on this.** Contract/unit coverage for: the streaming happy path (deltas accumulate to the full text; terminal chunk carries correct usage); served-model rejection on the first chunk (no `delta` reaches the caller); a transport failure before any content (plain `LLMError`, matching `complete()`); a transport failure after partial content (`LLMStreamInterrupted` with the correct accumulated `partial_text`); and policy parity (the same `provider_routing` fields as `complete()`, minus the schema-specific ones, are actually sent). Integration coverage for the run/journal state machine specifically: empty evidence creates zero `runs`/`llm_calls` rows; a successful call creates exactly one `ask` run and one `llm_calls` row with `status='succeeded'` regardless of `citations_unverified`; a pre-content `LLMError` leaves `status='failed'`; a post-content `LLMStreamInterrupted` leaves `status='partial'` with the partial text present in `llm_calls.response_raw` but never surfaced in the returned `AskAnswer`.

**Citation validation, after the stream completes.** The full answer text is scanned for citation markers matching `\[(\d+)\]`. Each marker is resolved against the evidence packet's `index` values:

- A marker resolving to a real evidence item becomes a validated `AskReference` (carrying that item's `document_id`/`revision_id`/`title`) in the response's `references` list.
- A marker with no matching `index` (a model inventing a citation number, or citing evidence trimmed by `ask_evidence_top_k`) is dropped from `references` and left as plain, non-clickable text in the answer — never surfaced as if it were a real citation, and never causes the request to fail.

**`citations_unverified` is set whenever `references` ends up empty, full stop — not only when markers were present and none resolved.** A model that ignores the citation instruction entirely and returns plain, marker-free prose is exactly as ungrounded as one whose markers all fail to resolve; gating the flag on "markers existed" would let the more common failure (no markers at all) return as if it were a normal, fully-cited answer, silently reintroducing the untraceable-claim problem this contract exists to close. Concretely: `citations_unverified = (len(references) == 0)`, computed after validation regardless of how many markers, if any, appeared in the text. `docs/DESIGN.md` 1's "every generated claim is traceable" requirement is enforced by this flag, not by trusting the prompt instruction to be followed.

**Traceability.** A validated `AskReference` names a `document_id`/`revision_id`; that revision's `revision_sources` rows (section 6.3) already link it to the raw `thought_id`s that produced it. Resolving an Ask citation down to raw source thoughts — the same traceability `docs/DESIGN.md` 1's Release 1 definition of done requires for every generated claim — reuses this existing table; no new provenance mechanism is needed.

**Degradation.** If `TC_MODEL_ASK`'s call itself fails before any content streams, or is interrupted mid-stream (`LLMStreamInterrupted`, above) after evidence was found (embedding search succeeded, generation did not complete), Ask returns `degraded: true` with the evidence list still attached and no partial, unvalidated prose — a caller falls back to showing raw search results rather than an answer that never went through citation validation. See `docs/adr/0010` for the full replacement design and migration path.

## 8. Embedding integration contract

`docs/adr/0010` retires Khoj; this section describes the self-hosted
embedding/search replacement it authorizes. It supersedes the prior Khoj
markdown-export/filename-convention contract, which lived in this section
through design version 1.8.

### 8.1 What the embedding sidecar owns

- Computing embedding vectors for document revision content, locally, via `EmbeddingPort`.
- Nothing else. It holds no index, no conversation state, and no filter logic — it is a stateless `embed(texts) -> vectors` call.

### 8.2 What pgvector (in the first-party PostgreSQL) owns

- Storage of one embedding vector per document's current revision, queried by exact (not approximate-index-accelerated) cosine-similarity ranking in v1 — see 8.4 for why.
- Semantic nearest-neighbor search, expressed as an ordinary SQL query alongside the exact-search predicates (7.5), not a separate system's query language.

### 8.3 What neither owns

- Raw thoughts, attachments, identities, entities, document revision history, schedules, provenance, exact filters, or backups — unchanged from the Khoj era.
- The canonical current version of a generated document.
- Authorization decisions for Discord or the future application UI.
- Reranking or RAG conversation flow — Ask's chat call goes to `OpenRouterProvider` (7.6), not to the embedding sidecar.

### 8.4 Stored row shape

Every document has at most one row, keyed by `document_id` — not `revision_id` — carrying whichever revision was most recently embedded successfully:

```sql
CREATE TABLE document_embeddings (
  document_id uuid PRIMARY KEY REFERENCES documents(id),
  revision_id uuid NOT NULL,
  embedding vector(384) NOT NULL,
  embedding_model_id text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (revision_id, document_id)
    REFERENCES document_revisions (id, document_id)
);
```

(Exact column list and vector dimensionality are finalized in the Slice 1
migration; `vector(384)` matches `thenlper/gte-small`'s output
dimensionality, `docs/adr/0010` §5's recommended model.) There is no
`workspace_id` column here — `workspace_id` is never stored redundantly
against a row it could drift out of sync with; every query joins
`document_embeddings -> documents` to obtain and filter on it, so a
row's workspace identity is exactly whatever `documents` already says it
is, not a second, independently writable foreign key that could disagree
with the first.

The `(revision_id, document_id)` pairing is enforced by a **composite**
foreign key against `document_revisions (id, document_id)` — not two
independent single-column foreign keys, one to `documents(id)` and one to
`document_revisions(id)` (the shape an earlier draft of this ADR used).
Two independent foreign keys can each individually reference something
that exists while still jointly describing a lie — `document_id` pointing
at document A, `revision_id` pointing at a real revision that actually
belongs to document B — because neither column's constraint knows the
other column exists. A faulty sync writer could then pair the wrong
document with the wrong revision and Postgres would accept it. The
composite foreign key closes exactly that gap: `document_revisions (id,
document_id)` is already unique (6.3's `UNIQUE (id, document_id)`, added
alongside this same fix), so the database itself refuses any
`document_embeddings` row whose `revision_id` does not actually belong to
its `document_id` — this is not merely handled in application code, it is
unrepresentable in the table. The same composite-foreign-key technique
was applied to `documents.current_revision_id` (6.3): that FK has
referenced only `document_revisions(id)` since ADR-0004, which is enough
to guarantee the referenced row exists but not that it belongs to *this*
document — a distinct but analogous ownership gap, closed here (`FOREIGN
KEY (current_revision_id, id) REFERENCES document_revisions (id,
document_id)`) once designing `document_embeddings`'s guarantee made the
general pattern visible.

**Currentness is enforced two ways, not by a single foreign key,**
because embedding sync is asynchronous by design (7.2 step 11's decoupled
outbox) and cannot be forced into the same transaction as the revision
write that supersedes a prior one:

1. **Write-path guard against stale overwrite.** `EmbeddingSyncLoop`'s
   writer upserts by `document_id`
   (`INSERT ... ON CONFLICT (document_id) DO UPDATE ...`) and only applies
   the update when the incoming `document_revisions.revision_number` is
   strictly greater than the currently stored row's revision number
   (compared via a join in the same statement). An at-least-once,
   out-of-order outbox redelivery for an already-superseded revision can
   therefore never overwrite a newer embedding with an older one.
2. **Read-path guard against sync lag.** Every semantic-search query joins
   on `document_embeddings.revision_id = documents.current_revision_id` as
   a hard filter, not merely `document_embeddings.document_id =
   documents.id`. A document whose embedding sync has not yet caught up to
   its latest revision is simply absent from semantic results until it
   does — the existing "a revision becomes searchable only after
   successful index acknowledgement" behavior (6.5) — rather than ever
   appearing with superseded content attached to a current-looking result.

There is no exported Markdown file or filename convention in this design —
the embedding is computed directly from the revision's stored
`body_markdown`, read at sync time, not from a stale copy captured when
the outbox event was originally enqueued.

**No vector index in v1 — queries use an exact scan, deliberately.** A
naive combination of an HNSW index with the workspace/date/entity/type/
source filters 7.5 requires would be wrong, not just suboptimal: pgvector
applies an HNSW-accelerated `ORDER BY embedding <=> $1 LIMIT $n` as an
*approximate* graph traversal first, then filters the candidates it
happened to find — a selective filter (a narrow date range, a specific
entity) can therefore return fewer than `$n` results, or miss a real
match entirely, even when valid matches exist elsewhere in the table,
without ever marking the response degraded. This is a documented,
general property of pgvector's HNSW filtering (its own guidance is
iterative index scans, partial indexes, or partitioning for exactly this
case), not a bug to route around case by case.

Given this project's stated scale — single workspace, thousands not
millions of `document_embeddings` rows (`docs/adr/0010` §1, matching
`docs/DESIGN.md` 7.3.6's cost envelope) — the correct answer for v1 is to
not build an approximate index at all: the semantic query (7.5) is a
plain `ORDER BY embedding <=> $1 LIMIT $n` with the same workspace/date/
entity/type/source predicates as the exact-search channel in the same
`WHERE` clause, executed as an ordinary sequential (or filter-index-
accelerated, for the non-vector predicates) scan that computes the exact
distance for every row matching those filters and sorts. There is no
approximation step to under-return results, by construction — "precise
before impressive" (section 3, principle 6) applies to the index
strategy itself, not only to the filters. At this row count the scan
costs low tens of milliseconds, well within acceptable interactive search
latency; there is no measured problem an ANN index would be solving yet.

**Revisit only when a stated trigger fires**, not preemptively: once
`document_embeddings` grows past the low tens of thousands of rows, or
measured semantic-search p95 latency (14.2's existing "search latency by
channel" metric) exceeds an owner-set threshold — whichever comes first.
At that point, adopt `CREATE INDEX document_embeddings_hnsw_idx ON
document_embeddings USING hnsw (embedding vector_cosine_ops)` together
with pgvector's own documented mitigation for filtered ANN search —
`SET LOCAL hnsw.iterative_scan = strict_order` (preserves exact distance
ordering while scanning additional candidates until the filter is
satisfied or a bound is hit) with a bounded `hnsw.max_scan_tuples` — and
add regression tests exercising recall under a deliberately selective
filter (a narrow date range or single-entity filter against a corpus
sized so that fewer than `hnsw.ef_search` candidates would satisfy it)
before enabling the index for production queries. Building the index
before that point, or building it without the iterative-scan setting and
its regression test, is exactly the silently-degraded-recall failure mode
this section exists to avoid.

### 8.5 Upgrade policy

Pin the embedding model by name and the sidecar image by digest, matching this project's existing pinned-digest-plus-contract-test policy (unchanged principle from the Khoj era). Changing the embedding model requires a `reembed` run (`runs.kind = 'reembed'`, `runs.embedding_model_id`) that recomputes every `document_embeddings` row before the new model is read from; never compare vectors from different models. Contract tests for the sidecar cover its `embed` endpoint's request/response shape and failure modes, mirroring the discipline `tests/contract/khoj` previously applied to the Khoj container.

## 9. Retrieval model

### 9.1 Exact path

- Date and time: B-tree predicates on `client_created_at`, local date, and local time.
- Entity: join through `entity_mentions` and aliases.
- Exact phrase: escaped substring check with trigram candidate index.
- English terms: `websearch_to_tsquery('english', ...)` plus `ts_rank_cd`.
- Fuzzy spelling: trigram similarity, opt-in and thresholded.
- Filters: workspace, source, document kind, run, revision state, and provenance IDs.

### 9.2 Semantic path

Embedding runs locally via a first-party sidecar service (`docs/adr/0010`), not Khoj: a small, pinned Python 3.12 process wrapping `sentence-transformers`, called through `EmbeddingPort` and stored in the first-party PostgreSQL's pgvector extension (8.4). This avoids sending document content to a third-party embedding API by default, matching this section's original intent from the Khoj era. If hardware performance is unacceptable, `EmbeddingPort` is designed to accept a future OpenRouter-backed adapter implementing the same protocol (`docs/adr/0010` §4) — a new adapter class, not a redesign of search or storage. Model changes require a full `reembed` run (8.5); never compare vectors from different models.

### 9.3 Multilingual upgrade seam

Persist BCP-47 language tags per thought and model IDs per index generation. The English FTS configuration is selected by a mapping function, not hard-coded throughout repositories. A future migration may add language-specific generated columns or external tokenization for languages without whitespace segmentation. Embedding configuration is workspace/index-version scoped. Multilingual support therefore requires a new tokenizer/FTS strategy and a `reembed` run (8.5), not a schema rewrite.

## 10. API contract

All first-party endpoints are under `/v1`, return RFC 9457-style problem details on errors, accept/emit UTC timestamps with offsets, and include `request_id`. Release 1 uses a local bearer token for non-Discord access. Workspace comes from authentication, never an untrusted query parameter.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/thoughts` | API/import capture with `Idempotency-Key` |
| `GET` | `/v1/thoughts` | paginated raw log with exact filters |
| `GET` | `/v1/thoughts/{id}` | raw thought and archival attachment metadata |
| `POST` | `/v1/thoughts/{id}/corrections` | append a correction event |
| `GET` | `/v1/documents` | list current documents |
| `GET` | `/v1/documents/{id}` | current revision and sources |
| `GET` | `/v1/documents/{id}/revisions` | revision timeline |
| `GET` | `/v1/documents/{id}/diff` | unified diff between two revisions |
| `POST` | `/v1/documents/{id}/restore` | create a new revision from a historical revision |
| `POST` | `/v1/runs/organize` | force organization; optional window, dry-run, provisional |
| `GET` | `/v1/runs/{id}` | status, models, prompts, usage, errors |
| `GET` | `/v1/search` | exact/semantic/hybrid normalized search |
| `POST` | `/v1/ask` | streamed Ask (self-hosted embedding evidence plus OpenRouter generation, `docs/adr/0010`) with validated citations and explicit degradation status |
| `GET` | `/v1/entities` | entities, aliases, document links |
| `POST` | `/v1/entities/{id}/aliases` | owner-approved alias |
| `POST` | `/v1/admin/embedding-sync` | force a re-embed of current documents: enqueues one `embedding.sync_requested` outbox event per document with a current revision (7.2 step 11, `docs/adr/0010`), the same event the organize writer enqueues on each revision write, so a forced sync and an organize-triggered sync share one code path; returns the count enqueued, not a synchronous embed |
| `POST` | `/v1/admin/export` | create portable plaintext export |
| `GET` | `/health/live` | process liveness only |
| `GET` | `/health/ready` | database and required dependency readiness |

`GET /v1/search` parameters include `q`, `mode`, `from`, `to`, `local_time_from`, `local_time_to`, `entity_id`, `kind`, `source`, `phrase`, `include`, `exclude`, `limit`, and opaque `cursor`. Results include `result_id`, `document_id`, `revision_id`, `thought_ids`, `title`, `snippet`, timestamps, entities, `channels` (`exact`, `semantic`), rank components, citation, and degradation state.

Discord slash commands map to use cases, not HTTP loopback calls:

- `/organize [from] [to] [dry_run]`
- `/search query`
- `/ask question`
- `/status [run_id]`
- `/undo document revision` (creates a restore revision after confirmation)

## 11. OpenRouter for a new AI developer

OpenRouter is an API gateway, not the model itself. The application sends an OpenAI-compatible HTTPS request to `https://openrouter.ai/api/v1` with an OpenRouter API key and a model slug. OpenRouter authenticates the project, routes the request to a provider that serves that model, normalizes the response, and bills the OpenRouter account. This allows model changes without replacing the SDK.

The first-party adapter uses the official OpenAI Python client pointed at OpenRouter's base URL. Configuration contains separate model IDs for `organize`, `select` (context assembly, section 7.3), `query_plan`, `ask` (`TC_MODEL_ASK`, section 7.6, `docs/adr/0010`), and optional `embedding`. Never use floating “latest” aliases in production; pin a tested slug and record the actual returned model/provider on every run.

Operational rules:

- Keep `OPENROUTER_API_KEY` only in environment/secret storage.
- Send optional app-identification headers but no personal identifier in them.
- Set explicit timeouts, retries with jitter, and a maximum token budget.
- Use structured outputs only with models verified to support the required schema behavior.
- Persist usage returned by the API and a dated price snapshot; price catalogs can change.
- Configure provider routing only after understanding whether prompts may be retained or used for training by the selected providers.
- Disable silent fallback between materially different models for organization unless the fallback model is explicitly tested.
- Cache development responses by a hash of redacted prompt, model, prompt version, and schema version; production capture content is not written to developer logs.

Model selection has two modes (ADR-0006, extended by `docs/adr/0010` to cover Ask). **Safe mode** (the default) restricts `model_organize`/`model_select`/`model_query_plan`/`model_ask` (`TC_MODEL_ASK`, `docs/adr/0010`) to a small allowlist of slugs that have actually been checked against the three rules above - retention/training policy, tested structured-output support, and the quoted-data prompt-injection assumption in section 12.2 - enforced at process startup, not left to review discipline. `REVIEWED_MODELS` entries are reviewed per stage (`ReviewedModel.stages`, as `organize`/`select`/`query_plan` already are); a slug pinned to `model_ask` must carry `"ask"` in its own `stages` list to be accepted - registry membership reviewed only for a different stage's prompt shape and cost profile does not transfer. It also disables OpenRouter provider fallback outright, sends `data_collection: deny` and `zdr: true` on every request, and restricts routing to the specific provider endpoint(s) recorded against the reviewed model (`provider.only`, since disabling fallback alone only blocks a *second* provider after the first fails, not OpenRouter's initial choice). `zdr: true` limits the request to OpenRouter's current zero-data-retention endpoints and fails the request rather than falling through to a non-ZDR endpoint. Together, these controls prevent a reviewed model from being silently served by an unreviewed backup, an unreviewed initial provider, or one that retains the request. `require_parameters: true` additionally excludes any provider that can't actually honor the strict schema enforcement being asked for. **Custom mode** lifts the model allowlist for an operator who accepts responsibility for an unreviewed model themselves, and leaves provider fallback to a separate boolean (`TC_MODEL_ALLOW_FALLBACK`, default on, matching OpenRouter's own default) without forcing retention-deny - which would otherwise break the one documented custom-mode case, a free tier that requires accepting training to use at all. Either way, the response actually served is still checked against the requested model before its content is used (see above): safe mode prevents sending prompts to a model nobody vetted; the served-model check catches a gateway silently substituting one after the fact. These are different failures and neither guard substitutes for the other.

`model_ask` joining the safe-mode-restricted field set is a Slice 1 code
change, not something this design authorizes retroactively: it means
extending `Settings._safe_mode_restricts_to_reviewed_models`'s
`field_stages` mapping (`packages/infrastructure/src/tc_infrastructure/
config.py`) with `"model_ask": "ask"`, adding a `TC_MODEL_ASK` field next
to `model_organize`/`model_select`/`model_query_plan`, and reviewing at
least one slug for the `"ask"` stage in `REVIEWED_MODELS`
(`tc_infrastructure.llm.reviewed_models`) before `TC_ASK_ENABLED=true` can
select a live model in safe mode. An empty `TC_MODEL_ASK` behaves like an
empty `model_organize` today: no provider call, not a validation failure.

The embedding model is the deliberate exception to the OpenRouter default: from the organize second wave onward it runs locally (section 7.3.6), because text embedding is cheap to host, keeps the organize path free of an extra network round trip, and avoids disclosing document content to a second provider.

Local models later implement the same `LLMProvider` port through an OpenAI-compatible server such as Ollama or vLLM. Switching is configuration plus evaluation, not business-logic work.

## 12. Privacy, security, and threat model

Personal memory data is unusually sensitive: it can reveal relationships, health, location, credentials accidentally pasted into chat, routines, and future plans. “Self-hosted” protects storage location but does not make outbound Discord and OpenRouter traffic private.

### 12.1 Data disclosures

- Discord sees message content and attachments sent through Discord.
- OpenRouter and the selected downstream model provider receive text included in model requests — including Ask's evidence-assembly prompt as of `docs/adr/0010`, which routes through the same reviewed-model/ZDR controls as `organize`/`select`/`query_plan` rather than a separate, ungoverned credential.
- Entity-document embeddings for organize context assembly are computed locally and are not disclosed to any provider (section 7.3.6). Search/Ask embeddings are likewise computed locally by the first-party sidecar (`docs/adr/0010`) and are not disclosed to any provider by default.
- If a future OpenRouter embedding adapter is enabled instead of the local sidecar, the provider receives indexed document content (`docs/adr/0010` §4's designed-for, not-yet-built seam).
- PostgreSQL (including its pgvector-backed semantic index) retains local copies.
- Off-site backup providers receive encrypted ciphertext only.

The settings and README must state these boundaries plainly. Release 1 has no claim of end-to-end encryption.

### 12.2 Required controls

- Allowlist exact Discord user, guild, and channel IDs; use least-privilege bot permissions.
- Enable only required Gateway intents. Do not request member or presence intents.
- Bind API, the embedding sidecar, and databases to `127.0.0.1` in local Compose.
- Use unique database roles/passwords.
- Keep secrets out of Git, logs, prompts, exports, and diagnostic bundles.
- Cap attachment size, sanitize filenames for display only, store by hash, reject executable types by policy, and scan before future processing.
- Encrypt host disks where available. Encrypt off-site backups with `age`; keep the private key off the backup destination.
- Redact body text from normal logs. Log IDs, sizes, timings, hashes where necessary, and error classes.
- Require confirmation for restore, export, entity merge, and future deletion operations.
- Use constant-time bearer-token comparison and rotate credentials through a runbook.
- Protect against prompt injection from captured text by treating it as quoted data in organization prompts; tools are disabled in organization calls.

### 12.3 Retention

Default retention is indefinite for raw thoughts and attachments because recoverability is a core goal. The pgvector-backed semantic index may be deleted and rebuilt via a `reembed` run (8.5). A future deletion feature requires a separate ADR covering backups, tombstones, Discord copies, and legal expectations; it must never be introduced as a routine cleanup.

### 12.4 Future VPS controls

Use Linux, automatic security updates, a firewall exposing only 80/443, Caddy or equivalent TLS termination, Tailscale or OIDC for private admin access, rootless/non-root containers, read-only filesystems where possible, resource limits, fail2ban/rate limits, monitored backups, and no public PostgreSQL/embedding-sidecar ports. VPS deployment is prepared but not executed in Release 1.

## 13. Local deployment

Development target is Windows with WSL2 and Docker Desktop; production-like execution occurs in Linux containers. Compose services use health checks, restart policies, named volumes, and an internal network. Only the gateway and the minimal operator debug UI (`docs/adr/0009`) bind to loopback host ports.

Suggested Compose profiles:

- `core`: postgres, api, bot, worker;
- `ai`: the embedding sidecar (`docs/adr/0010`);
- `observability`: optional local metrics/log tooling;
- `backup`: one-shot backup and restore-test jobs.

Migrations run as a one-shot `migrate` service before app rollout, never inside API startup. The worker uses PostgreSQL advisory locks so multiple replicas cannot organize the same window. Containers run as non-root and receive only their required volumes.

Configuration groups:

- Discord: token, owner user ID, guild/channel IDs.
- Workspace: name, timezone, 20:00 cutoff.
- Database: DSN and pool sizes.
- OpenRouter: key, base URL, tested model IDs (including `TC_MODEL_ASK`), budgets.
- Embedding sidecar: internal URL, pinned image digest, timeouts.
- Attachments: root, limits, allowed/blocked MIME policy.
- Retrieval: result limits, RRF constant, fuzzy threshold.

## 14. Reliability and operations

### 14.1 Failure behavior

| Failure | Required behavior |
|---|---|
| PostgreSQL unavailable at capture | no acknowledgement; retry; visible bot error if retry budget ends |
| Duplicate Discord delivery | return existing thought ID and idempotent acknowledgement |
| Attachment copy interrupted | no commit/ack; temporary file removed later |
| OpenRouter unavailable | run fails before derived commit; catch-up/manual retry |
| Invalid model JSON | one repair attempt, then fail loudly |
| Embedding sidecar unavailable | canonical documents commit; embedding sync retries; exact search remains available |
| Discord unavailable for digest | outbox retries without rerunning organization |
| Machine off at 20:00 | catch-up windows on startup |
| Worker crash mid-run | lease expires; retry inspects run state; no partial derived transaction |
| Prompt/model changes | new versioned run; prior revisions preserved |
| Very large window | deterministic chunking with overlap and final coverage validation |

### 14.2 Observability

Use structured JSON logs with request/run IDs and OpenTelemetry-compatible traces. Metrics include capture latency/errors, outbox age, unprocessed windows, run duration/status, coverage, embedding sync lag, search latency by channel, degraded queries, OpenRouter tokens/cost, attachment bytes, and backup age. Release 1 may expose Prometheus text metrics locally; alerts can initially be Discord owner messages with deduplication.

Never include raw bodies, generated documents, model prompts, API keys, or signed attachment URLs in logs.

### 14.3 Backup and export

- Nightly `pg_dump` after successful digest to a separate local backup directory.
- Nightly attachment manifest and incremental copy.
- Weekly encrypted off-site backup is designed but optional until a destination is selected.
- Monthly portable export: raw thoughts as timestamped Markdown/JSONL, current and historical document revisions as Markdown, provenance manifest, entities, and attachment checksums.
- Quarterly restore into scratch volumes, validate schema, row counts, foreign keys, blob hashes, and sampled documents. Record a `restore_test` run.

Backups have retention tiers (for example 14 daily, 8 weekly, 12 monthly) only after the owner approves the off-site provider and key custody plan.

## 15. Testing and evaluation

### 15.1 Test layers

- Unit: timezone/cutoff computation, normalization, idempotency decisions, revision construction, query planning, rank fusion, diff/revert behavior.
- Property: arbitrary Unicode text, timestamps around DST, repeated delivery, revision chains, and RRF invariants.
- Integration: PostgreSQL constraints/migrations/FTS, transactional outbox, attachment atomicity, OpenRouter mock server, Discord adapter fixtures.
- Embedding sidecar contract: pinned image, `embed` request/response schema, failure modes, unavailable behavior (`docs/adr/0010`, replacing the prior Khoj contract suite).
- End-to-end: Discord fixture -> capture -> forced organization -> revision -> embedding sync -> search -> Ask -> digest outbox.
- Security: authorization boundaries, secret scan, dependency scan, malicious filenames, oversized uploads, prompt-injection fixtures, log-redaction checks.
- Recovery: database/blob backup and automated scratch restore.

### 15.2 Retrieval golden set

Build at least 30 owner-written questions with expected thought/document IDs and filters. Include exact names, misspellings, date-only, time-of-day, entity, semantic paraphrase, negation, superseded revisions, and “no answer” cases. CI runs deterministic exact tests; scheduled/local evaluation runs pgvector-backed semantic recall@5 and MRR (`docs/adr/0010`) because model execution may be slower.

### 15.3 Organization evaluation

- Context recall: every entity document referenced by the output had its body supplied to the prompt, not only its index row (section 7.3.5).
- Coverage: every input thought is cited or explicitly unorganized.
- Grounding: sampled claims are supported by cited raw text.
- Temporal correctness: relative dates resolve from source timestamp/timezone.
- Revision quality: changes do not erase unsupported previous facts; conflict is represented, not silently resolved.
- Entity quality: alias precision and unresolved-candidate rate.
- Regression diff: compare the same windows across prompt/model versions.

Model or prompt promotion requires passing fixed thresholds and human review of a representative diff set.

## 16. CI/CD and engineering workflow

GitHub Actions (or equivalent) on every change:

1. secret scan and dependency metadata validation;
2. Ruff format/check;
3. mypy strict for domain/application packages;
4. pytest unit/property tests;
5. PostgreSQL integration tests with migrations from empty and previous release;
6. embedding sidecar contract smoke test against the pinned digest;
7. build non-root OCI images and generate an SBOM;
8. vulnerability scan with explicit severity policy.

Tags build immutable images. A later VPS pipeline backs up, runs migrations, deploys Compose, checks readiness, runs a smoke capture/search, and retains the previous image for rollback. Database rollback uses forward-fix migrations unless a tested down migration is safe; raw data is never reset.

Branches are short-lived. Any implementation that changes an accepted architecture decision adds `docs/adr/NNNN-title.md`, updates this design's decision index, and increments the design version. Prompt changes are reviewed like code.

## 17. Repository boundaries

```text
apps/api                  FastAPI routes, streaming, dependency wiring
apps/discord_bot          Discord event/command transport only
apps/worker               scheduler and outbox consumer entrypoints
services/embedding_sidecar  first-party Python 3.12 FastAPI service, own pyproject.toml/Dockerfile (docs/adr/0010)
packages/domain           immutable domain types, policies, ports
packages/application      capture, organize, search, ask, restore use cases
packages/infrastructure   postgres, filesystem, Discord, OpenRouter, embedding-sidecar adapters
prompts                   versioned system/user templates and JSON schemas
migrations                first-party Alembic history
deploy/compose            base, local, and future VPS overlays
tests/unit                pure logic
tests/integration         database/adapters
tests/contract/embedding  pinned embedding-sidecar behavior
tests/eval                golden retrieval and organization fixtures
docs/adr                  implementation decisions
```

No third-party retrieval or chat service source is vendored — the embedding sidecar is entirely first-party code. Generated memories, attachments, local exports, caches, databases, secrets, and rendered design intermediates are ignored by Git.

## 18. Delivery plan and gates

### Phase 0 - Repository and durable capture

Create package skeleton, Compose PostgreSQL, migrations, workspace seed, Discord adapter, attachment archive, outbox, and capture tests. Gate: replay/failure tests prove acknowledged messages are durable and deduplicated.

### Phase 0.5 - Habit validation

Use text capture for two weeks without LLM organization changes. Gate: owner still captures and friction observations are recorded. This protects against building retrieval for an unused corpus.

### Phase 1 - Organization and Discord digest

Add OpenRouter adapter, versioned prompts, runs, entity extraction, daily/entity revisions, 20:00 scheduler, `/organize`, digest outbox, and evaluation fixtures. Gate: five representative windows rerun without destructive change; coverage and grounding pass.

### Phase 2 - Self-hosted embedding and hybrid retrieval

Add pgvector to the first-party PostgreSQL image, build the embedding sidecar and `EmbeddingPort`, implement embedding sync, exact search, semantic search, RRF, an Ask path routed through `OpenRouterProvider`/`TC_MODEL_ASK`, and contract tests (`docs/adr/0010`, superseding this phase's original Khoj-based plan from design version 1.8 and earlier). Gate: golden-set recall and degradation behavior pass at parity with or better than the superseded Khoj-era baseline before Khoj is removed.

### Phase 3 - Operational hardening

Add backup/export/restore drill, metrics, budgets, security tests, upgrade runbooks, and two-week unattended operation. Gate: recovery objectives and failure matrix are demonstrated.

### Phase 4 - Unified custom UI

Build the browser client against `/v1` only. Include search filters, Ask streaming/references, raw source view, revisions/diff/restore, run status, and settings. Gate: the embedding sidecar or `TC_MODEL_ASK`'s provider may be upgraded or temporarily unavailable without changing the UI contract; exact search remains usable.

### Phase 5 - VPS readiness and multi-user implementation

Only on owner request: choose provider, TLS/private access, monitoring, encrypted off-site backup, and deployment automation. Implement real authentication, PostgreSQL row-level security, workspace invitation/membership, per-workspace model budgets, and data export/deletion policies. Shared “hivemind” workspaces and isolated personal workspaces use the same schema.

## 19. Accepted decisions and deferred decisions

### Accepted

- Discord replaces Telegram as the first capture surface.
- Single-user operation with workspace-scoped schema.
- Raw log and original attachments are append-only canonical data.
- PostgreSQL handles precision retrieval and, as of `docs/adr/0010`, semantic retrieval (via a self-hosted embedding sidecar and pgvector) and Ask (via `OpenRouterProvider`/`TC_MODEL_ASK`); Khoj is retired.
- First-party services use Python 3.14; the embedding sidecar is a separately versioned Python 3.12 process, runtime-isolated for the same reason Khoj previously was (`docs/adr/0010`, pending a `cp314` torch wheel).
- OpenRouter is the LLM gateway for organize, select, query_plan, and ask; local models remain a compatible future adapter.
- Daily digest cutoff defaults to 20:00 local, using successful-cutoff capture windows.
- Entity documents evolve through immutable full-snapshot revisions.
- A unified custom UI is Phase 4, calling the first-party `/v1` gateway only; there is no Khoj UI dependency to sequence around.
- Organize context is assembled from a complete entity index plus additive body selection, with locally computed embeddings as a second-wave signal.

### Deferred with explicit trigger

- Exact model slugs: select during implementation by a small structured-output evaluation and budget check.
- Off-site backup provider: select before VPS or after one month of valued data, whichever comes first.
- Reranking and document chunking for semantic search: explicitly deferred by `docs/adr/0010`; revisit only if golden-set retrieval quality proves insufficient without them.
- Cloud embedding adapter: designed for (`EmbeddingPort`, `docs/adr/0010` §4) but not built; trigger is unacceptable local sidecar performance on the owner's hardware.
- OCR/vision/transcription: new release only after capture habit validation.
- Entity merge across weak aliases: require review workflow and quality data.
- Cross-day non-entity rolling documents: evaluate after entity documents are used.
- Agentic tool-calling retrieval in the organize pipeline: reconsider only when the complete entity index no longer fits the prompt budget, in the high hundreds of entity documents. Below that threshold the index is already fully visible to the model, while a loop costs roughly 2.5-5x the assembly input tokens and makes the section 15.3 regression diffs non-reproducible.
- Public or multi-user access: requires Phase 5 security gate.

## 20. Initial ADR index

| ADR | Decision |
|---|---|
| ADR-0001 | Append-only canonical event log |
| ADR-0002 | Discord Gateway as capture adapter |
| ADR-0003 | PostgreSQL precision retrieval plus Khoj semantic/Ask — **superseded by ADR-0010** for the semantic/Ask half; the PostgreSQL-precision-retrieval half remains accepted |
| ADR-0004 | Immutable full-snapshot document revisions |
| ADR-0005 | Workspace-scoped single-user-first schema |
| ADR-0006 | OpenRouter behind a provider port, with safe and custom model-selection modes |
| ADR-0007 | Custom unified UI over stable gateway |
| ADR-0010 | Retire Khoj — self-hosted embedding (pgvector + sidecar) and Ask via `OpenRouterProvider`/`TC_MODEL_ASK` |

Implementation should create these ADR files when the first code for each decision lands; this design remains the summary authority. (ADR numbers 0008-0009 exist under `docs/adr/` for decisions not yet reflected in this index table; this table is not exhaustive of every accepted ADR, only the ones tracked here since Release 1 planning.)

## 21. References

Verified 2026-08-30, retained for historical context of the retired Khoj-era design (`docs/adr/0003`, superseded by `docs/adr/0010` for semantic search/Ask):

- Khoj query filters (word, date, and file): https://docs.khoj.dev/miscellaneous/query-filters/
- Khoj semantic search and configurable local/OpenAI-compatible models: https://docs.khoj.dev/features/search/
- Khoj Ask/RAG behavior and references: https://docs.khoj.dev/features/chat/
- Khoj self-hosting: https://docs.khoj.dev/get-started/setup/
- Khoj source/package compatibility and AGPL license: https://github.com/khoj-ai/khoj

Verified 2026-09-08, current architecture (`docs/adr/0010`):

- pgvector extension, exact scans, and HNSW's documented filtered-search/iterative-scan behavior (8.4): https://github.com/pgvector/pgvector
- sentence-transformers: https://www.sbert.net/
- `thenlper/gte-small` model card: https://huggingface.co/thenlper/gte-small
- PyTorch Python 3.14 wheel tracking issue: https://github.com/pytorch/pytorch/issues/156856

Verified 2026-08-30, still current:

- Discord Gateway and privileged message-content intent: https://docs.discord.com/developers/events/gateway
- Discord message/attachment behavior: https://docs.discord.com/developers/resources/message
- OpenRouter OpenAI-compatible quickstart: https://openrouter.ai/docs/quickstart
- OpenRouter embeddings endpoint: https://openrouter.ai/docs/api/api-reference/embeddings/create-embeddings
- Python 3.14.7 release: https://www.python.org/downloads/release/python-3147/

## Appendix A - Release 1 acceptance checklist

- [ ] Fresh clone starts with documented commands and no manually created database objects.
- [ ] Secrets are supplied externally and secret scanning passes.
- [ ] Unauthorized Discord content is neither stored nor acknowledged.
- [ ] Text and allowed attachments survive process termination after acknowledgement.
- [ ] Duplicate Discord events return the existing thought.
- [ ] 20:00 cutoff and DST tests pass for at least Bangkok and London.
- [ ] Catch-up produces exactly one canonical result per closed window.
- [ ] `/organize` dry run writes no derived revisions.
- [ ] Every revision has run, parent, checksum, change summary, and thought sources.
- [ ] Restore creates a new revision and preserves the full chain.
- [ ] Exact search covers every documented filter.
- [ ] Hybrid search exposes channel scores and degraded state.
- [ ] Semantic (pgvector) index contains current revisions only.
- [ ] Ask references resolve to current documents and raw sources.
- [ ] Strict filters are either honored or rejected explicitly.
- [ ] OpenRouter failure, timeout, invalid JSON, and budget limit are tested.
- [ ] Logs contain no raw thought bodies, tokens, or signed URLs.
- [ ] Backup/export restore succeeds in scratch volumes.
- [ ] Golden retrieval thresholds and organization evaluation gates pass.

## Appendix B - Definition of a safe change

A change is safe when raw data remains readable, existing revisions remain reachable, migrations work from every supported release, current Markdown can be rebuilt deterministically, the semantic index can be rebuilt from canonical revisions via a `reembed` run, API compatibility is preserved or versioned, and the relevant failure/recovery test passes. If any condition is unknown, the change is not ready to merge.
