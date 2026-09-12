# Khoj retirement and replacement — completion plan (ADR-0010)

- **Status:** draft for owner approval
- **Authority:** `docs/adr/0010-self-hosted-embedding-search-ask.md` (Accepted), `docs/DESIGN.md` 7.5, 7.6, 8, 9.2, 10, 15.2
- **Baseline commit:** `origin/main` @ `eacca4a` (PRs #30, #31, #32 merged)
- **Audience:** this document is written to be *executed by delegated agents*. Each task below is a self-contained brief: owned files, contract, tests, acceptance, and explicit do-not-touch boundaries. The primary session owns triage, architectural decisions, and the final diff review; it does not need to write the code.

---

## 0. Where the work actually stands

ADR-0010's "Migration and rollback" names five forward steps. Measured against `origin/main`:

| ADR step | State | Evidence |
|---|---|---|
| 1. pgvector image + `document_embeddings` migration | **Done** | `deploy/compose/postgres/Dockerfile`, `migrations/versions/0008_document_embeddings.py`, `packages/infrastructure/src/tc_infrastructure/db/tables.py:262` |
| 2a. Embedding sidecar service | **Done** | `apps/embedding_sidecar/**`, `tests/contract/embedding_sidecar/**`, compose service `embedding-sidecar` on the internal `embedding` network |
| 2b. `EmbeddingPort` + `EmbeddingSyncLoop` | **Not started** | no `tc_domain/embedding_ports.py`, no `tc_infrastructure/embedding/`, no worker loop; nothing writes a `document_embeddings` row outside tests |
| 3. `EmbeddingSearchPort` into `Search._semantic_results` behind a flag | **Not started** | `packages/application/src/tc_application/search.py` still calls `KhojPort.search` + `SemanticHydrator` |
| 4. `TC_MODEL_ASK` + `LLMProvider.stream()` Ask path | **Not started** | `packages/domain/src/tc_domain/llm.py` has no `stream()`/`LLMStreamChunk`/`LLMStep.ASK`; `config.py` has no `model_ask` |
| 5. Cutover + Khoj removal | **Not started** | gate redefined by §2 Decision B — removal is now gated on an automated correctness bar, not a Khoj comparison |

Two gaps already visible on `origin/main`:

1. **Config declared but never read.** `env.example:166-167` ships `TC_EMBEDDING_SIDECAR_PORT` / `TC_EMBEDDING_SIDECAR_BASE_URL`, but `packages/infrastructure/src/tc_infrastructure/config.py` has no corresponding `Settings` field. Closed by **T2.2**.
2. **Two unmerged commits** sit on `feat/slice-1-pgvector-embedding-sidecar` after PR #31 merged: `283b44b` (sidecar cross-field resource bounds vs uvicorn's real accounting) and `bd12ca7` (DESIGN.md 7.3.6's stale "pgvector not installed" claim). Closed by **T0.1**.

Repo hygiene, not a code task: the checkout at the repository root is on `main`, **32 commits behind `origin/main`**, carrying a *stale staged index* that is an older snapshot of already-merged PR #31/#32 work. Nothing unique appears to be at risk, but no agent should be pointed at that checkout until it is reconciled — see **T0.0**.

---

## 1. Outcome and non-goals

**Outcome.** Semantic search and Ask are served entirely by first-party code: the local embedding sidecar computes vectors, the first-party PostgreSQL's pgvector column stores and ranks them under the same SQL `WHERE` clause as exact search, and Ask generates over that evidence through `OpenRouterProvider` with a reviewed `TC_MODEL_ASK` slug and validated citations. The Khoj container, adapter, credentials, and `khoj_sync` machinery are gone.

**Non-goals (ADR §1 — do not let an agent widen into these).**

- `organize` / `select` / `query_plan` stay on OpenRouter cloud, untouched.
- No reranking model.
- No document chunking — one vector per document's current revision.
- No cloud embedding adapter (the `EmbeddingPort` seam is built; the adapter is not).
- No HNSW or any approximate vector index. v1 is an exact scan, deliberately (ADR §2, DESIGN 8.4).

---

## 2. Owner decisions — all resolved 2026-09-11

All four are settled. They are recorded here with their reasoning because each one is a deviation from, or a narrowing of, something the accepted ADR says — an agent reading only the ADR would implement something different.

### Decision A — `EmbeddingVector` shape — **RESOLVED: frozen dataclass**

ADR §4 sketches `EmbeddingVector(BaseModel)`. But `packages/domain/src/tc_domain/llm.py` states the domain layer is **stdlib only** ("Provider SDKs live behind `tc_infrastructure`"), and every other domain value type is a frozen `@dataclass(slots=True)`.

**Ruling:** frozen dataclass, matching `LLMResponse`/`SearchResult`. The ADR's `BaseModel` is illustrative pseudocode, not a literal instruction; importing `pydantic` into `tc_domain` would break an accepted layering invariant for the first time and would itself require an ADR. Record the deviation in the Slice 2 commit report — do not silently diverge, and do not "fix" the domain type to match the ADR snippet later.

### Decision B — the Khoj-removal gate — **RESOLVED: no pre-removal comparison**

ADR-0010's Verification section gated full Khoj removal on *"passes `docs/DESIGN.md` 15.2's golden-set recall/MRR thresholds at parity with or better than the Khoj-era baseline."*

**That gate is dropped.** It cannot be satisfied and, on the owner's reasoning, should not be manufactured:

- **There is no data to measure against.** The local development database's contents were destroyed (`docs/incidents/0001-docker-compose-down-deleted-the-real-dev-stack.md`). There is no corpus of organized documents for either channel to retrieve from.
- **No Khoj-era baseline was ever captured.** "Parity with the baseline" compares against a number that does not exist and now cannot be reconstructed.
- **The retirement is architectural, not quality-driven.** ADR-0010's three stated reasons are integration cost against an opaque system, an ungoverned second LLM credential, and the wrong shape of dependency for embedding. None of them is "Khoj retrieves badly." Gating an architectural decision on a retrieval-quality comparison it was never motivated by would be ceremony, not evidence.

**What replaces it.** Removal is gated on the automated correctness bar this plan already requires — not on a comparison:

- **T3.4**, the selective-filter recall test: a synthetic corpus (no real data needed) proving semantic search returns *every* matching row within the requested limit under a deliberately narrow filter. This is the ADR §2 exact-scan guarantee, machine-checked.
- **T4.11**, the Ask run/journal state-machine tests, and the citation-validation tests.
- Appendix A's directly relevant items: the semantic index contains current revisions only; Ask references resolve to current documents and raw sources; hybrid search exposes channel scores and degraded state.

**The forward path for quality, when there is data.** Ask calls are journaled from Slice 4 onward — one `runs` row (`kind='ask'`) and one `llm_calls` row per call, carrying the prompt version, the full answer, the model served, and usage (DESIGN 7.6, T4.7). Once real documents exist, a model or retrieval comparison can be run *from the journal*, against live traffic, without needing either system stood up for the purpose. That is a better comparison than the one being skipped, and it is not blocked by removing Khoj.

**Two obligations this does NOT discharge:**

1. **An ADR-0010 addendum is still required** (**T0.2**). Changing an accepted acceptance criterion must be written down, per this repository's convention of stating a deviation rather than silently rewriting prior text. This is a paragraph, not a new ADR.
2. **DESIGN 15.2's golden set remains a Release 1 obligation.** Appendix A's "Golden retrieval thresholds and organization evaluation gates pass" is a Release 1 acceptance item in its own right, independent of this cutover. Decision B **decouples** it from Slice 5; it does not delete it. It is owed once there is a corpus to build it against. Do not let its absence from Slice 5 be read as its cancellation.

**Residual risk, accepted knowingly:** the new path drops Khoj's cross-encoder reranking and all document chunking (ADR §1). Both are accepted simplifications, neither has been measured, and after removal there is no side-by-side window in which to measure them. The mitigations are T3.4's correctness floor, Decision D's truncation counter, and the Ask journal above.

### Decision C — fate of `khoj_index_items` — **RESOLVED: drop the table**

Migration `0007_khoj_index_items.py` created a table whose only purpose was recording Khoj's last successful sync. The new design has no analogue — currentness is enforced by `document_embeddings.revision_id = documents.current_revision_id` (DESIGN 8.4).

**Ruling:** drop it in the Slice 5 removal migration (**T5.3**). It holds zero rows and no canonical data — it is derived bookkeeping for a system being deleted. `downgrade()` recreates the empty table in `0007`'s shape; there is no data to restore, so the "refuse rather than destroy" guard `0008` needs does not apply here. Leaving a dead table would tax `tests/integration/test_schema_drift.py` and every future reader forever.

### Decision D — embedding input truncation — **RESOLVED: add a truncation tracker**

`thenlper/gte-small` has a 512-token sequence limit, and `sentence-transformers` **silently truncates** beyond it. The sidecar accepts up to `max_text_length = 50_000` characters. With no chunking (ADR §1), a long document's embedding represents only its opening ~512 tokens — invisibly. Exact search (keyword, phrase, date, entity) is unaffected; only the semantic channel is.

**Ruling:** do **not** add chunking — ADR §1 forbids it in v1, and chunking is the source of the duplicate-hit bug class this design escapes. Do make truncation *visible*: the sidecar returns a per-text `truncated` flag and the sync writer logs a counter, never content (**T2.9**). The ADR's stated revisit trigger then becomes a number rather than a hunch.

**Owner's accepted rationale:** most documents are expected to be short enough to fit; for the long ones that do truncate, the owner can generally recall an important document by name, and exact search finds it in full. The counter exists so that assumption can be checked against reality later rather than assumed permanently.

---

## 3. Rules every delegated agent must follow

Load-bearing, not stylistic. From `CLAUDE.md` and `docs/incidents/0001`.

1. **One branch/worktree per slice.** Never two agents editing the same checkout concurrently. Parallel agents within a slice must own **disjoint file sets** — the file lists in each task below are exclusive ownership boundaries.
2. **Never modify a file another task in the same round owns.** If you need a change there, stop and report it; do not edit it.
3. **Exclusive resources are serial.** `uv.lock`, `apps/embedding_sidecar/uv.lock`, `scripts/check.sh`, `scripts/check.ps1`, `.github/workflows/ci.yml`, `env.example`, `deploy/compose/docker-compose.yml`, and any single migration file may be touched by **at most one task per round**.
4. **Docker safety.** Never `docker compose down -v` with an implicit project name. Use `scripts/compose-teardown.sh` / `.ps1` with an explicit `-p <throwaway-name>`. Give throwaway containers, projects, and ports values distinct from the real dev stack. Run a read-only inspection (`docker ps -a`, `docker volume ls`) in the *same turn* as any destructive command. Run `scripts/backup.sh`/`.ps1` before manual verification that could touch the real dev stack.
5. **Checks.** Run the smallest relevant check during work; run `./scripts/check.ps1` (PowerShell) or `./scripts/check.sh` (Bash) before declaring a task complete. Any new test suite or tool **must** be added to both check scripts and `.github/workflows/ci.yml` in the same change. Never weaken or skip a failing check to claim completion.
6. **Fixtures use synthetic memory content only.** No real personal content and no secrets in tests, logs, diagnostics, or commit messages.
7. **Workspace scoping is non-negotiable** even single-user: every new query, port, and endpoint is workspace-scoped, and `workspace_id` comes from auth context, never a request parameter.
8. **Acknowledgement after durable commit**, append-only canonical data, derived artifacts versioned and reversible. Do not trade these for convenience.
9. **Commit reports.** Every commit body uses `CLAUDE.md`'s required structure (What changed / Why / Technical details / Interfaces and data / Verification / Risks and recovery / Follow-ups), naming new functions and endpoints and what they do. `None` for genuinely inapplicable categories; never omit a heading.
10. **Codex independent review before merge** for anything touching persistence, migrations, security, privacy, retrieval, model prompts, backups, or deployment — which is every slice here.

---

## 4. Slice 0 — Prerequisites

### T0.0 — Reconcile the primary checkout (owner action, not an agent task)

The repository-root checkout is on `main`, 32 commits behind `origin/main`, with a stale staged index reproducing an older snapshot of merged PR #31/#32 work. Untracked and **not** on `origin/main`: `.agents/skills/model-candidate-screening/SKILL.md`, `.claude/agents/fix-implementer.md`, `.claude/agents/integration-reviewer.md`, `.claude/skills/first-line-review/**`.

Before any agent is pointed at this checkout: preserve those untracked files, then bring the checkout to `origin/main`. **The owner should perform or explicitly authorize this** — it discards a staged index, which is not an agent's call to make.

### T0.1 — Land the two unmerged Slice-1 commits

- **Depends on:** T0.0
- **Owns:** nothing new; cherry-picks `283b44b`, `bd12ca7`
- **Do:** branch `fix/slice-1-followups` off `origin/main`, cherry-pick both commits in order, open a PR. `283b44b` is a real correctness fix to the sidecar's cross-field resource-bound validation; `bd12ca7` corrects a DESIGN.md sentence that still claims pgvector is not installed.
- **Accept:** both commits on `origin/main`; check script green; `feat/slice-1-pgvector-embedding-sidecar` fully merged and deletable.

### T0.2 — Record the gate change as an ADR-0010 addendum

- **Depends on:** nothing (Decision B is settled); should land before Slice 5 is planned, and may land immediately
- **Owns:** `docs/adr/0010-self-hosted-embedding-search-ask.md`
- **Do:** append a dated addendum amending the Verification section's removal gate. State plainly: the golden-set/MRR parity comparison is dropped; why (no corpus exists after the incident-0001 data loss, no Khoj baseline was ever captured, and the retirement's three stated motivations are architectural rather than retrieval-quality); what replaces it (T3.4's selective-filter recall test, T4.11's run/journal and citation-validation tests, and the relevant Appendix A items); and the forward path (Ask's `runs`/`llm_calls` journal makes a later model/retrieval comparison possible from live traffic without either system stood up for the purpose).
  - **Also state explicitly** that DESIGN 15.2's golden set remains an open Release 1 obligation (Appendix A, "Golden retrieval thresholds and organization evaluation gates pass") and is merely decoupled from Slice 5, not cancelled.
  - Append; do not rewrite the original Verification text. This repository's convention is to record a deviation beside the prior decision, not in place of it.
- **Accept:** a reader of ADR-0010 alone reaches the same gate this plan describes, and can see that the golden set is still owed later.

---

## 5. Slice 2 — `EmbeddingPort` + `EmbeddingSyncLoop` (ADR step 2b)

**Goal.** Every organize write, and an admin force-sync, results in a `document_embeddings` row for the document's current revision, computed by the sidecar. Khoj sync keeps running unchanged alongside it. **No read path changes in this slice** — nothing queries the new rows yet.

**Branch:** `feat/slice-2-embedding-sync`

**Ordering.** T2.1 lands first (everything imports it). Then T2.2, T2.3, T2.9 in parallel. Then T2.4. Then T2.5 and T2.6 in parallel. Then T2.7, then T2.8 (exclusive resources, last).

### T2.1 — Domain ports and value types

- **Owns:** `packages/domain/src/tc_domain/embedding_ports.py` (new)
- **Do:** define, stdlib-only frozen dataclasses/Protocols per Decision A:
  - `EmbeddingVector(values: tuple[float, ...], model_id: str, dimensions: int)`
  - `EmbeddingPort` Protocol: `async def embed(self, texts: tuple[str, ...]) -> tuple[EmbeddingVector, ...]` — one vector per input, in order; returns `()` for an empty tuple without calling out.
  - `EmbeddingUnavailableError(Exception)` — mirrors `KhojUnavailableError`'s role: the search/Ask layer catches it and sets `degraded=true`, never swallows it.
  - `PendingEmbeddingSync(event_id, workspace_id, document_id, attempts)` — carries `document_id` only, never `revision_id`; the consumer re-reads the current revision at delivery time (same reasoning as `PendingKhojSync`'s docstring).
  - `EmbeddingSyncOutbox` Protocol: `claim(limit)`, `mark_delivered(event_id)`, `mark_failed(event_id, *, attempts, error)`.
  - `EmbeddingSource` Protocol: `get_revision(*, workspace_id, document_id) -> RevisionForEmbedding(document_id, revision_id, revision_number, title, body_markdown)`. Raises unless the document resolves **and** belongs to that workspace — never on `document_id` alone.
  - `EmbeddingWriter` Protocol: `upsert(*, workspace_id, document_id, revision_id, revision_number, vector) -> bool` — returns whether the row was actually written (False when a strictly-newer revision is already stored).
  - `EmbeddingForceSyncPort` Protocol: `enqueue_all(workspace_id) -> int`.
- **Tests:** none of its own (pure declarations); `tests/unit/test_embedding_ports.py` may assert `runtime_checkable` conformance of the stubs used elsewhere.
- **Accept:** `mypy` clean; no import of `pydantic`, `sqlalchemy`, or `httpx` anywhere in the file.
- **Do NOT:** touch `tc_domain/search.py` or `tc_domain/ask.py` — Slice 3/4 territory.

### T2.2 — HTTP adapter and settings

- **Depends on:** T2.1
- **Owns:** `packages/infrastructure/src/tc_infrastructure/embedding/__init__.py`, `.../embedding/client.py` (new), and the embedding block in `packages/infrastructure/src/tc_infrastructure/config.py`
- **Do:**
  - `HttpEmbeddingClient(http, base_url, *, timeout)` implementing `EmbeddingPort` against the sidecar's `POST /embed` (`{"texts": [...]}` → `{model_id, model_revision, dimensions, vectors}`).
  - Map **every** failure to `EmbeddingUnavailableError`: transport error, timeout, non-2xx (**including 503 model-loading and 429 capacity**), malformed or missing JSON keys, vector count not matching input count, `dimensions` not matching the expected 384. Never let a bare `KeyError`/`TypeError`/`ValueError` escape — ADR §1 names this exact bug class as one Khoj forced us to relearn; do not reintroduce it.
  - `EmbeddingVector.model_id` is the **combined identity** `f"{model_id}@{model_revision}"`. The sidecar's `schemas.py` explicitly defers this composition to the writer, and a mutable HF branch name alone is not a model identity. Define the composition in one function used by both the client and the writer.
  - Add to `Settings`: `embedding_sidecar_base_url: str = "http://embedding-sidecar:8081"`, `embedding_sidecar_timeout_seconds: float = Field(default=30.0, gt=0)`, `embedding_sync_batch_size: int = Field(default=20, ge=1)`. The in-container default differs from `env.example`'s loopback value, which exists only for the contract-test overlay — say so in the field comment.
  - Never log request or response bodies (12.2). Log `error_class` only, as `DeliverKhojSync` already does.
- **Tests:** `tests/unit/test_embedding_client.py` — happy path; each failure mode above maps to `EmbeddingUnavailableError`; vector-count and dimension mismatch rejected; timeout honored. `tests/unit/test_settings_embedding.py` — defaults and overrides.
- **Accept:** `uv run pytest tests/unit -k embedding` green; `env.example`'s `TC_EMBEDDING_SIDECAR_*` variables are now actually read by `Settings`.
- **Do NOT:** touch `env.example` (T2.8 owns it) or any `db/` module (T2.3 owns those).

### T2.3 — Persistence: outbox, source, writer, force-sync

- **Depends on:** T2.1
- **Owns:** `packages/infrastructure/src/tc_infrastructure/db/embedding_sync_enqueue.py`, `embedding_sync_outbox.py`, `embedding_source.py`, `embedding_writer.py`, `embedding_force_sync.py` (all new), plus the **one-line dual enqueue** at `db/organize_writer.py:112`
- **Do:**
  - `EMBEDDING_SYNC_REQUESTED_EVENT = "embedding.sync_requested"`; `enqueue_embedding_sync(session, *, workspace_id, document_id)` — modeled directly on `khoj_sync_enqueue.py`.
  - `PostgresEmbeddingSyncOutbox` — `PostgresOutbox.claim(limit, event_types=(EMBEDDING_SYNC_REQUESTED_EVENT,))`, same lease/backoff shape as `PostgresKhojSyncOutbox`. A malformed payload is failed immediately rather than returned.
  - `PostgresEmbeddingSource.get_revision` — joins `documents -> document_revisions` on `documents.current_revision_id`, filtered by `workspace_id`; returns `revision_number`, `title`, `body_markdown`.
  - `PostgresEmbeddingWriter.upsert` — one statement: `INSERT INTO document_embeddings (...) VALUES (...) ON CONFLICT (document_id) DO UPDATE SET ... WHERE <incoming revision_number> > (SELECT revision_number FROM document_revisions WHERE id = document_embeddings.revision_id)`. The strictly-greater guard is **required** (DESIGN 8.4, write-path guard 1): an at-least-once, out-of-order outbox redelivery must never overwrite a newer embedding with an older one. Return whether a row was affected.
  - `PostgresEmbeddingForceSync.enqueue_all` — one `embedding.sync_requested` event per document with a current revision, mirroring `khoj_force_sync.py`; returns the count. Enqueue only; delivery is the loop's job.
  - In `organize_writer.py`, enqueue **both** `khoj.sync_requested` and `embedding.sync_requested` in the same transaction as the revision write. Both paths run in parallel until Slice 5 (ADR step 2: "running alongside Khoj sync, not replacing it yet"). This is the only line of that file this task may change.
- **Tests:**
  - `tests/integration/test_embedding_sync_outbox.py` — claim scoping (an `embedding.sync_requested` claim never returns a `khoj.sync_requested` event, and vice versa), lease/backoff, malformed payload failed not returned.
  - `tests/integration/test_embedding_writer.py` — fresh insert; newer revision replaces; **older revision does not replace**; cross-document `revision_id` rejected by the composite FK (extend `tests/integration/test_document_embeddings.py`'s existing FK test rather than duplicating it); `updated_at` advances only on a real write.
  - `tests/integration/test_embedding_source.py` — workspace scoping: another workspace's `document_id` raises.
- **Accept:** those integration tests green against a real pgvector Postgres; `organize_writer`'s existing tests still green.
- **Do NOT:** touch `khoj_*.py` modules, `tables.py` (already correct), or any migration.

### T2.4 — Application use cases

- **Depends on:** T2.1, T2.2, T2.3
- **Owns:** `packages/application/src/tc_application/embedding_sync.py` (new)
- **Do:** `DeliverEmbeddingSync` — one poll cycle: claim due events; per event read the current revision, embed `body_markdown` (DESIGN 8.4 specifies the body, not a title+body concatenation, unless the owner rules otherwise), upsert, mark delivered. Keep `mark_delivered` **inside** the same `try` as the write, for the reason `DeliverKhojSync._sync_one`'s comment already documents — an exception escaping the handler would leave the event neither delivered nor failed and strand the rest of the claimed batch. On any exception: log `error_class` only, `mark_failed` with backoff, continue the batch. Return the count synced. Also `ForceEmbeddingSync(enqueuer)`.
- **Tests:** `tests/unit/test_embedding_sync.py` with fake ports — happy path; sidecar unavailable → `mark_failed`, not delivered; writer returning False (stale revision) still marks the event **delivered**, not failed (the event was handled correctly; there was nothing newer to write); a source-lookup failure marks failed; one failing event does not abort the rest of the batch; the recorded error string never contains document content.
- **Accept:** `uv run pytest tests/unit -k embedding_sync` green.
- **Do NOT:** touch `khoj_sync.py`.

### T2.5 — Worker loop and composition

- **Depends on:** T2.4
- **Owns:** `apps/worker/src/tc_worker/embedding_sync_loop.py` (new), and the embedding wiring block in `apps/worker/src/tc_worker/__main__.py`
- **Do:** `EmbeddingSyncLoop`, structurally identical to `KhojSyncLoop` (poll interval, cancellation, a poll-cycle exception logged but never killing the loop). Wire it in `serve()` alongside — not replacing — `khoj_sync_loop`: construct `HttpEmbeddingClient` against `settings.embedding_sidecar_base_url` on the existing shared `http` client, start both loops, stop both on shutdown. The `worker` service is already attached to the internal `embedding` network (`deploy/compose/docker-compose.yml:314`), so no compose change is needed here.
- **Tests:** `tests/unit/test_embedding_sync_loop.py` — starts/stops idempotently; a raising deliver call does not terminate the loop; cancellation propagates.
- **Accept:** unit tests green; the worker starts with an unreachable sidecar and logs rather than crashing (an unreachable sidecar must degrade, never take the worker down).
- **Do NOT:** touch the digest or organize wiring in `__main__.py`.

### T2.6 — `POST /v1/admin/embedding-sync`

- **Depends on:** T2.4
- **Owns:** `apps/api/src/tc_api/routers/admin.py`, the `ApiContext` field in `apps/api/src/tc_api/dependencies.py`, the response model in `apps/api/src/tc_api/schemas.py`, and the API composition root that builds them
- **Do:** add `POST /v1/admin/embedding-sync` returning `{"enqueued": n}` (DESIGN 10 names this endpoint exactly). **Keep `POST /v1/admin/khoj-sync` working unchanged** — it is removed in Slice 5, not here. Workspace comes from auth context.
- **Tests:** extend `tests/integration/test_api_admin.py` — the new endpoint enqueues one event per current document and returns the count; it is bearer-token protected; the existing khoj-sync endpoint still behaves identically.
- **Accept:** `uv run pytest tests/integration/test_api_admin.py` green.

### T2.7 — Contract test for the adapter against the real sidecar

- **Depends on:** T2.2
- **Owns:** `tests/contract/embedding_sidecar/test_embedding_client.py` (new)
- **Do:** exercise `HttpEmbeddingClient` (not raw HTTP — that is the existing `test_embed_endpoint.py`'s job) against the real running container via the existing contract-test overlay: a real batch returns the right count and dimensionality; an over-limit batch and an over-length text surface as `EmbeddingUnavailableError`, not a raw `HTTPStatusError`; the same text embeds deterministically across two calls. Reuse `tests/contract/embedding_sidecar/conftest.py`'s existing skip-when-unreachable gating — do not invent a second gating mechanism.
- **Accept:** passes with the contract-test compose overlay up; skips cleanly when it is not.

### T2.8 — Checks, CI, env, docs

- **Depends on:** all of T2.1–T2.7 (touches exclusive resources; last task in the slice)
- **Owns:** `scripts/check.sh`, `scripts/check.ps1`, `.github/workflows/ci.yml`, `env.example`, `docs/DEVELOPMENT.md`, `docs/OPERATING.md`
- **Do:** ensure the new unit/integration/contract tests are actually selected by both check scripts and CI — a suite neither check script runs is a real defect, and this exact failure already happened once (commit `3db5a77`). Document `TC_EMBEDDING_SIDECAR_BASE_URL`'s two correct values (in-container service name vs loopback for contract tests) in `env.example`. Add the embedding sync loop and the new admin endpoint to `docs/OPERATING.md`'s runbook.
- **Accept:** `./scripts/check.sh` **and** `./scripts/check.ps1` both green end to end; CI green.

### T2.9 — Make truncation observable (Decision D — approved)

- **Depends on:** nothing; file-disjoint from T2.2–T2.8, but touches the sidecar's own separate toolchain (Python 3.12, own lockfile), so run its checks with `cd apps/embedding_sidecar && uv run ...`
- **Owns:** `apps/embedding_sidecar/src/tc_embedding_sidecar/schemas.py`, `.../model.py`, `apps/embedding_sidecar/tests/test_model.py`, `tests/contract/embedding_sidecar/test_embed_endpoint.py`
- **Do:** have the sidecar report, per input text, whether the tokenizer truncated it (compare tokenized length against the model's `max_seq_length` before encoding). Add `truncated: tuple[bool, ...]` to `EmbedResponse`. The sync writer logs a count of truncated documents, never content. This makes ADR §1's stated chunking-revisit trigger measurable instead of invisible.
- **Accept:** the sidecar's own `uv run pytest` green; the contract test asserts a >512-token synthetic text reports `truncated: true` and a short one reports `false`; the field is additive, so T2.2's client tolerates its absence.
- **Do NOT:** add chunking. ADR §1 forbids it in v1.

**Slice 2 done when:** an organize run writes both outbox events; the worker embeds and upserts; `POST /v1/admin/embedding-sync` backfills; a stale redelivery cannot overwrite a newer vector; truncated documents are counted in the logs (T2.9); both check scripts green; Codex review obtained (persistence + deployment surface).

---

## 6. Slice 3 — `EmbeddingSearchPort` behind a flag (ADR step 3)

**Goal.** `Search` can serve its semantic channel from pgvector instead of Khoj, selected by config, with both paths present and comparable. Default stays Khoj.

**Branch:** `feat/slice-3-embedding-search`

### T3.1 — Domain port

- **Owns:** `packages/domain/src/tc_domain/embedding_ports.py` (extend)
- **Do:** `EmbeddingSearchPort` Protocol: `async def search(self, workspace_id, query: SearchQuery, vector: EmbeddingVector) -> tuple[SearchResult, ...]`. It returns fully-normalized `SearchResult`s — **one step, not two** (ADR §6): no hydrator, no filename, no re-resolution. Document in the docstring that filters are applied in the same SQL `WHERE` clause as the distance ordering, which is exactly what makes a separate re-checking pass unnecessary.
- **Do NOT:** delete `SemanticHydrator` from `tc_domain/search.py` — it stays until Slice 5.

### T3.2 — The pgvector query

- **Depends on:** T3.1
- **Owns:** `packages/infrastructure/src/tc_infrastructure/db/embedding_search_reader.py` (new)
- **Do:** one SQL query — `document_embeddings` joined to `documents` (for `workspace_id`, which is **never** stored on the embedding row) and to `document_revisions`, with:
  - `documents.workspace_id = :workspace_id`;
  - `document_embeddings.revision_id = documents.current_revision_id` — the hard read-path currentness guard (DESIGN 8.4, guard 2). A document whose sync lags is simply absent, never surfaced with stale content under a current-looking title;
  - every structured filter from `SearchQuery` — date/local-time window on source thoughts, `entity_id`, `kind`, `source`, `phrase`, `include`, `exclude` — in the **same** `WHERE` clause. Reuse the predicate builders in `db/search_reader.py` rather than writing a second, divergent copy of the filter semantics; if they need extracting into a shared module, that extraction is part of this task and `search_reader.py`'s behavior must not change;
  - `ORDER BY embedding <=> :query_vector LIMIT :limit` — **exact scan, no index** (ADR §2, DESIGN 8.4). Do not add `CREATE INDEX ... USING hnsw`. Do not add `SET LOCAL hnsw.*`;
  - `q` itself is deliberately **not** applied as a lexical predicate — a semantic hit need not literally contain the query text; that is what makes it semantic (see `SemanticHydrator`'s existing docstring for the precedent);
  - populate `rank` from the cosine distance (converted to a similarity) and `channels=("semantic",)`.
- **Tests:** `tests/integration/test_embedding_search_reader.py` — workspace isolation (another workspace's matching row is never returned); a superseded-revision row is excluded; each filter genuinely narrows results; ordering is by distance.
- **Do NOT:** change `search_reader.py`'s existing behavior, or add any vector index.

### T3.3 — Wire into `Search` behind a flag

- **Depends on:** T3.2
- **Owns:** `packages/application/src/tc_application/search.py`, `Settings.semantic_channel` in `config.py`, and the API/worker/bot composition roots that construct `Search`
- **Do:** add `semantic_channel: Literal["khoj", "embedding"] = "khoj"` to `Settings`. `Search` takes both an optional Khoj pair and an optional `(EmbeddingPort, EmbeddingSearchPort)` pair, and `_semantic_results` dispatches on the configured channel. The embedding path: `embed(text)` → `EmbeddingSearchPort.search(...)`. `EmbeddingUnavailableError` degrades exactly as `KhojUnavailableError` does today — `degraded=True`, never an empty success (DESIGN 7.5). RRF fusion (`fuse_rrf`, `k=60`) and the cursor rejection for `semantic`/`hybrid` are **unchanged**.
- **Tests:** extend `tests/unit/test_search.py` — both channels selectable; embedding-path degradation; fusion output identical in shape across channels; cursor still rejected.
- **Note for the reviewer:** the filename-dedup logic in `_semantic_results` applies only to the Khoj path. The embedding path must **not** carry it over — one row per document makes duplicates structurally impossible (ADR §6), and a defensive dedup here would hide a real bug if one ever occurred.

### T3.4 — Selective-filter recall test (required by ADR Verification)

- **Depends on:** T3.2
- **Owns:** `tests/integration/test_embedding_search_recall.py` (new)
- **Do:** seed a synthetic corpus large enough that an approximate index *would* under-return, apply a deliberately selective filter (a single entity, or a narrow date range matching a small subset), and assert semantic search returns **every** matching row within the requested limit. This is both the correctness baseline now and the regression test ADR §2 requires to keep passing unmodified if HNSW is ever added later. Say so in the module docstring, citing ADR §2 / DESIGN 8.4's trigger.
- **Accept:** green, and a reviewer can tell from the file alone why deleting or relaxing it would be a mistake.

> **Removed by Decision B.** An earlier draft had a T3.5 building a Khoj-vs-pgvector comparison harness. There is no corpus to run it against and no Khoj baseline to compare to, so it is cancelled — not deferred. DESIGN 15.2's golden set is still owed for Release 1 (see Decision B, obligation 2), but it is not part of this migration and does not gate Slice 5.

### T3.5 — Checks/CI/docs for Slice 3

- **Depends on:** T3.1–T3.4
- **Owns:** `scripts/check.sh`, `scripts/check.ps1`, `.github/workflows/ci.yml`, `env.example`, `docs/OPERATING.md`
- Same rule as T2.8: every new suite runs in both scripts and in CI.

**Slice 3 done when:** `TC_SEMANTIC_CHANNEL=embedding` serves semantic and hybrid search from pgvector with all filters honored; degradation is explicit; the selective-filter recall test is green; the default is still Khoj; Codex review obtained (retrieval surface).

---

## 7. Slice 4 — Ask on `OpenRouterProvider` (ADR step 4)

The largest slice, and the only one introducing a new provider contract. **Read DESIGN 7.6 in full before starting** — it is the normative specification and is more detailed than ADR §7–§9.

**This slice carries extra weight under Decision B.** Because no pre-removal retrieval comparison will be run, the Ask journal built here (one `runs` row + one `llm_calls` row per call, with prompt version, full answer, model served, and usage) is the project's *only* future mechanism for comparing retrieval or model quality once real documents exist. Treat T4.7's journaling and T4.11's state-machine tests as load-bearing, not as bookkeeping.

**Branch:** `feat/slice-4-ask-openrouter`

### T4.1 — Streaming provider contract (domain)

- **Owns:** `packages/domain/src/tc_domain/llm.py`
- **Do:** add exactly what DESIGN 7.6 specifies: `LLMStreamChunk(delta, finish_reason, model_served, generation_id, input_tokens, output_tokens, cost_usd)`; `LLMStreamInterrupted(LLMError)` carrying `partial_text` plus whatever usage was known; `LLMProvider.stream(request) -> AsyncIterator[LLMStreamChunk]`; `LLMStep.ASK = "ask"`. `complete()` is **not** modified — organize/select/query_plan stay non-streaming.
- **Accept:** `mypy` clean; every existing `LLMProvider` implementation still satisfies the Protocol (see T4.3).

### T4.2 — `OpenRouterProvider.stream()`

- **Depends on:** T4.1
- **Owns:** `packages/infrastructure/src/tc_infrastructure/llm/openrouter.py`
- **Do:** send the same `messages`/`temperature`/`max_tokens` and the **same `provider_routing` policy dict** `complete()` already builds (`allow_fallbacks`, `data_collection: deny`, `zdr: true`, `provider.only`), minus the schema-specific fields (`require_parameters`, `response_format`) — Ask's output is free-text prose. Plus `stream=True` and `stream_options={"include_usage": true}`.
  - **The served-model check runs on the first chunk, before any `delta` reaches the caller.** An unapproved substituted model raises `LLMError` with zero tokens emitted.
  - A transport/timeout/status failure **before** any content-bearing chunk raises plain `LLMError`. The same failure **after** at least one non-empty `delta` raises `LLMStreamInterrupted` carrying the accumulated text.
  - Extract the shared policy-dict construction into one function used by both `complete()` and `stream()`, so the two cannot drift apart.
- **Tests:** `tests/unit/test_openrouter_stream.py` — happy path (deltas accumulate; terminal chunk carries usage); served-model rejection with no delta delivered; pre-content failure → `LLMError`; post-content failure → `LLMStreamInterrupted` with the correct `partial_text`; **policy parity** (assert the same `provider_routing` fields as `complete()`, minus the schema ones, are actually sent). This list is ADR-mandated, not discretionary.

### T4.3 — Offline provider conformance

- **Depends on:** T4.1
- **Owns:** `packages/infrastructure/src/tc_infrastructure/llm/offline.py`, `llm/factory.py`
- **Do:** implement `stream()` deterministically on the offline adapter — an empty `TC_MODEL_ASK` must select it rather than silently calling a provider (`model_organize`'s existing convention). Keep the factory's selection logic consistent.

### T4.4 — Migration 0009: `'ask'`

- **Owns:** `migrations/versions/0009_ask_runs_and_calls.py` (new)
- **Do:** widen `runs.kind`'s CHECK to add `'ask'` and `llm_calls.step`'s CHECK to add `'ask'`. **Purely additive — never narrow either constraint here**, and do not touch `'khoj_sync'` (Slice 5's migration, and only after cutover is proven). `runs.status` already permits `'partial'` (`0004:54`), so no status change is needed. Data-safe `downgrade()` in `0008`'s shape: refuse rather than destroy if rows depend on the value being dropped.
- **Tests:** extend `tests/integration/test_migration_roundtrip.py` and `test_schema_drift.py`; assert an `'ask'` run and an `'ask'` `llm_calls` row insert successfully, and that `'khoj_sync'` is **still** accepted.

### T4.5 — Settings, reviewed model, safe-mode gate

- **Depends on:** T4.1
- **Owns:** `packages/infrastructure/src/tc_infrastructure/config.py`, `packages/infrastructure/src/tc_infrastructure/llm/reviewed_models.py`
- **Do:** add `model_ask: str = ""` and `context_ask_evidence_top_k: int = Field(default=5, ge=1)` (DESIGN 7.6 names it `context.ask_evidence_top_k`, alongside the existing `context_*` settings). Add `"model_ask": "ask"` to `_safe_mode_restricts_to_reviewed_models`'s `field_stages` map so an unreviewed or wrong-stage slug **fails process start**, not the first `/ask` call. Add at least one `REVIEWED_MODELS` entry whose `stages` includes `"ask"`, reviewed against §8's citation-contract prompt shape specifically — this is a real screening task (see `.claude/skills/model-candidate-screening`, whose current scope is organize/select and will need an `ask` stage), not a one-line registry edit.
  - **Keep `ask_provider_retention_acknowledged` in this slice**, but re-scope it: required only while Ask routes to Khoj. Removing it now would break the Khoj Ask path that must stay usable for rollback until Slice 5. Its full removal, and its `env.example` entries, belong to **T5.4**.
- **Tests:** extend the settings tests — an unreviewed `TC_MODEL_ASK` fails startup; a slug reviewed only for `organize` fails for `ask`; an empty slug is exempt.

### T4.6 — Ask domain types

- **Depends on:** T4.1
- **Owns:** `packages/domain/src/tc_domain/ask.py`
- **Do:** add `AskEvidenceItem(index, document_id, revision_id, title, snippet)` — 1-based `index`, stable for one request only. Extend `AskReference` with `revision_id`. Add `citations_unverified: bool` and `evidence: tuple[AskEvidenceItem, ...]` to `AskAnswer`/`AskChunk`. Rewrite the docstrings: `strict_unsupported` no longer applies to the semantic channel — strict filters are now ordinary SQL predicates and are always honored (DESIGN 7.6). Decide and document whether `strict_unsupported` is retained as a now-always-False field for API compatibility or removed; **recommend** removing it from the domain type and having the API layer emit `false` until a versioned API change, so the field cannot quietly mean two different things.

### T4.7 — `AskQuestion` rewrite

- **Depends on:** T4.2, T4.5, T4.6, and Slice 3's `EmbeddingSearchPort`
- **Owns:** `packages/application/src/tc_application/ask.py`
- **Do:** implement DESIGN 7.6's sequence exactly:
  1. Not enabled → a single `enabled=False` chunk, no work done.
  2. Assemble evidence: `EmbeddingPort.embed(question)` → `EmbeddingSearchPort.search(...)` → top `context_ask_evidence_top_k` hits → `AskEvidenceItem`s with a **bounded** snippet of `body_markdown`, never the full body.
  3. **Empty evidence short-circuits before any model call and before any `runs` row exists** — a fixed, non-generated `"No indexed documents matched this question."` with `evidence: []`. Zero `runs`, zero `llm_calls`.
  4. Non-empty evidence → insert one `runs` row (`kind='ask'`, `status='running'`, `model_id=TC_MODEL_ASK`, `prompt_version`, `window_start`/`window_end` NULL) **before** calling `stream()`.
  5. Stream, accumulating text. Terminal chunk → write one `llm_calls` row (`step='ask'`, `sequence=1`, accumulated text in `response_raw`, usage from the terminal chunk) and resolve the run to `succeeded`. Pre-content `LLMError` → one `llm_calls` row with `error_code` and no content, run `failed`. `LLMStreamInterrupted` → one `llm_calls` row with the partial text in `response_raw` and `error_code` set, run `partial`, and the returned `AskAnswer` reports `degraded: true` **without surfacing the partial prose** — it never went through citation validation.
  6. Citation validation after the stream: scan for `\[(\d+)\]`; a marker resolving to a real evidence `index` becomes a validated `AskReference`; a marker resolving to nothing is dropped and left as inert text, never a working citation, and never fails the request.
  7. `citations_unverified = (len(references) == 0)`, **computed unconditionally after validation** — never gated on whether markers were present. A marker-free answer and an all-invalid-marker answer are the same grounding failure.
  8. `citations_unverified` **never** changes the run's status. A clean call with zero valid citations is a `succeeded` run.
- **Do NOT:** reintroduce a filename-based dedup or a `SemanticHydrator` call. Both belonged to Khoj's chunking.

### T4.8 — Prompt

- **Depends on:** T4.6
- **Owns:** the new Ask prompt module (alongside the existing organize prompts) and its `prompt_version`
- **Do:** instruct the model to answer **only** from the numbered evidence items and to mark every evidentiary claim with an inline `[n]` matching that item's index. Use the existing "quoted data, not instructions" framing the organize prompts already use (12.2) — evidence is untrusted data, never instructions. Version the prompt; it is journaled.

### T4.9 — Run ledger `partial()`

- **Depends on:** T4.4
- **Owns:** `packages/infrastructure/src/tc_infrastructure/db/run_ledger.py`
- **Do:** `PostgresRunLedger` currently has `start`/`succeed`/`fail` only. Add `partial(run_id, *, error_code, error_detail, ...usage)` and ensure `start(kind="ask")` carries `model_id` and leaves the window columns NULL. Copy `model_provider`/token/cost fields up from the `llm_calls` row on `succeed`, as DESIGN 7.6 specifies.

### T4.10 — API and Discord surfaces

- **Depends on:** T4.7
- **Owns:** `apps/api/src/tc_api/routers/ask.py`, `apps/api/src/tc_api/schemas.py`, `apps/discord_bot/src/tc_discord_bot/commands.py`, and the composition roots constructing `AskQuestion`
- **Do:** extend the streamed `/v1/ask` response with `citations_unverified`, `evidence`, and per-reference `revision_id`; keep explicit degradation status. Discord `/ask` continues to drain the same stream via `collect_ask_answer` — **one use case, two surfaces**, no divergent journaling path (DESIGN 7.6 is explicit about this). Surface `citations_unverified` visibly to the user; an ungrounded answer must not look like a cited one.

### T4.11 — Run/journal state-machine integration tests (ADR-mandated)

- **Depends on:** T4.7, T4.9
- **Owns:** `tests/integration/test_ask_runs_and_journal.py` (new), `tests/integration/test_api_ask.py` (extend)
- **Do:** exactly the four cases the ADR names: empty evidence creates **zero** `runs`/`llm_calls` rows; a successful call creates exactly one `ask` run and one `llm_calls` row with `status='succeeded'` **regardless of `citations_unverified`**; a pre-content `LLMError` leaves `status='failed'`; a post-content `LLMStreamInterrupted` leaves `status='partial'` with the partial text present in `llm_calls.response_raw` but **never** in the returned `AskAnswer`. Plus citation-validation tests: a fabricated or out-of-range marker never becomes a returned `AskReference`; a marker-free answer sets `citations_unverified: true` exactly like an all-invalid-marker answer.

### T4.12 — Checks/CI/env/docs for Slice 4

- **Depends on:** T4.1–T4.11
- **Owns:** `scripts/check.sh`, `scripts/check.ps1`, `.github/workflows/ci.yml`, `env.example`, `docs/OPERATING.md`
- **Do:** document `TC_MODEL_ASK` and `TC_CONTEXT_ASK_EVIDENCE_TOP_K` in `env.example` with the same reviewed-model framing the other slugs use. Ensure new suites run in both scripts and CI.

**Slice 4 done when:** `/v1/ask` and Discord `/ask` answer from pgvector evidence through a reviewed `TC_MODEL_ASK` with validated citations, correct run/journal states, and no second credential; the Khoj Ask path is still selectable for rollback; Codex review obtained (model prompts + privacy + persistence).

---

## 8. Slice 5 — Cutover and Khoj removal (ADR step 5)

**Do not start until T0.2's addendum is written and T5.1's bar is green.** Per Decision B the gate is now an automated correctness bar rather than a Khoj comparison — but it is still a gate, and Khoj stays buildable until it passes.

**Branch:** `feat/slice-5-khoj-removal`

### T5.1 — Confirm the replacement gate

- **Depends on:** T0.2, and Slices 2–4 complete
- **Do:** demonstrate and record, in one place, that the replacement bar is green:
  - **T3.4** — the selective-filter recall test passes (semantic search returns every matching row within the limit under a deliberately narrow filter).
  - **T4.11** — the Ask run/journal state machine and citation-validation tests pass.
  - **Appendix A items this migration is responsible for:** the semantic index contains current revisions only; Ask references resolve to current documents and raw sources; hybrid search exposes channel scores and degraded state; strict filters are honored or rejected explicitly.
  - Both check scripts green, on both shells.
- **Do NOT:** attempt a Khoj-vs-pgvector retrieval comparison, or stand Khoj up to generate one. Decision B cancelled it; there is no corpus and no baseline, and manufacturing one would be ceremony rather than evidence.
- **Accept:** the above recorded in the PR description with command output, plus the owner's explicit go-ahead. The go-ahead is the gate, not a formality.

### T5.2 — Flip defaults

- **Owns:** `config.py`, `env.example`
- **Do:** `semantic_channel` defaults to `"embedding"`; Ask routes to `OpenRouterProvider` by default. Khoj remains reachable by explicit configuration for one release, per the ADR's rollback plan.

### T5.3 — Removal migration

- **Depends on:** T5.2
- **Owns:** `migrations/versions/0010_retire_khoj.py` (new)
- **Do:** two changes, both authorized here and nowhere earlier:
  - Drop `'khoj_sync'` from `runs.kind`'s CHECK. **This and only this migration** may narrow that constraint, and only now, because only now is it certain no in-flight or historical row depends on it.
  - **Drop `khoj_index_items`** (Decision C). It holds zero rows and no canonical data. `downgrade()` recreates the empty table in `0007`'s shape — there is nothing to restore, so `0008`'s "refuse rather than destroy" guard does not apply to this table. Still verify emptiness at migration time rather than assuming it: if any row is present, fail loudly rather than dropping it silently.
- **Tests:** migration round-trip; `'khoj_sync'` is now rejected while `'embedding_sync'` and `'ask'` are still accepted; `khoj_index_items` is gone after upgrade and present-but-empty after downgrade; `tests/integration/test_schema_drift.py` reflects both changes.

### T5.4 — Delete the code

- **Depends on:** T5.3
- **Owns:** all Khoj-named modules and their tests
- **Delete:** `packages/domain/src/tc_domain/{khoj_ports,khoj_sync_ports,khoj_export}.py`; `packages/application/src/tc_application/khoj_sync.py`; `packages/infrastructure/src/tc_infrastructure/khoj/`; `packages/infrastructure/src/tc_infrastructure/db/{khoj_force_sync,khoj_index_recorder,khoj_sync_enqueue,khoj_sync_outbox,semantic_hydrator}.py`; `apps/worker/src/tc_worker/khoj_sync_loop.py`; `tests/contract/khoj/`; `tests/unit/test_khoj_*.py`; `tests/integration/test_khoj_*.py` and `test_semantic_hydrator.py`. Remove `SemanticHydrator` and `has_strict_filters` from `tc_domain/search.py` if nothing else uses them (check `has_strict_filters` — Ask no longer needs it once strict filters are ordinary SQL). Remove `POST /v1/admin/khoj-sync` and its schema. Remove the dual enqueue in `organize_writer.py`, leaving only `embedding.sync_requested`. Remove `Settings.khoj_base_url`, `ask_provider_retention_acknowledged`, and `_ask_requires_provider_acknowledgment`.
- **Watch for:** `KhojUnavailableError` referenced from `tc_domain/errors.py` and `organize_ports.py`; `KhojExportNotFound`; Khoj mentions in `db/document_reader.py` and `db/search_reader.py` docstrings. Grep case-insensitively for `khoj` across `.py`, `.yml`, `.toml`, `.md`, `.sh`, `.ps1` and leave zero live references outside `docs/adr/0003` (historical), `docs/adr/0010` (which cites it deliberately), and this plan.

### T5.5 — Delete the infrastructure

- **Depends on:** T5.4
- **Owns:** `deploy/compose/khoj.docker-compose.yml` (delete), `deploy/compose/docker-compose.yml`, `env.example`, `scripts/check.sh`, `scripts/check.ps1`, `.github/workflows/ci.yml`, `docs/DEVELOPMENT.md`, `docs/OPERATING.md`, `README.md`, `AGENTS.md`
- **Do:** delete the `ai` profile and the `TC_KHOJ_*` compose-config-sanity assertions in both check scripts (the whole "must fail without `TC_KHOJ_*`" block), the Khoj contract-test gating, and every `TC_KHOJ_*` / `TC_ASK_PROVIDER_RETENTION_ACKNOWLEDGED` entry in `env.example`. Reclaim any CI steps that existed only for Khoj.
- **Accept:** a clean-machine `--profile core` bring-up needs no Khoj variable at all, and both check scripts pass with no Khoj-related skip.

### T5.6 — Documentation close-out

- **Depends on:** T5.5
- **Owns:** `docs/adr/0003-*.md`, `docs/adr/0010-*.md`, `docs/DESIGN.md`
- **Do:** mark ADR-0003 superseded in its own header (it is currently named as superseded only from ADR-0010's side). Update ADR-0010's `First implemented in:` from "not yet" to the shipped slices. Bump the DESIGN version and changelog. Record any deviation from the ADR text (Decisions A–D) explicitly rather than silently rewriting the ADR — this repository's stated convention.

**Slice 5 done when:** no Khoj container, code, credential, or configuration remains; `runs.kind` no longer accepts `'khoj_sync'`; `khoj_index_items` is dropped; ADR-0010 and ADR-0003 both read correctly to someone arriving fresh; both check scripts green from a clean clone; Codex review obtained (migration + deployment + privacy).

---

## 9. Suggested delegation shape

Per `CLAUDE.md` and `.claude/skills/first-line-review`:

- **Per slice:** one branch/worktree. Implementation agents run in parallel only where the task briefs give them disjoint file ownership; the exclusive-resource list in §3 rule 3 is what actually prevents collisions.
- **Per task:** a `fix-implementer`-style agent suits tasks that are already fully specified here — most of Slice 2 and Slice 5 are. The exceptions needing judgment and the owner: **T4.5**'s reviewed-model screening for the `ask` stage, **T4.8**'s prompt, and **T0.2**'s ADR addendum.
- **Per slice, before handoff:** run `first-line-review` (parallel shard reviewers plus one `integration-reviewer`, then triage). The `integration-reviewer` matters most in Slices 2 and 4, where the failure modes are cross-boundary: outbox event-type drift between enqueuer and claimer, acknowledgement ordered before durable commit, workspace scoping lost at a handoff, a new suite neither check script runs, and configuration declared but never read — the exact defect present on `origin/main` today.
- **Triage stays in the primary session.** A subagent finding becomes a fix only after the primary session reads the code and agrees the bug is real.
- **Codex independent review before merge** on every slice here.

---

## 10. Risks

- **Slice 4 concentrates the risk.** A new streaming provider contract, a new run kind, a new journal state, and a citation-validation contract land together because DESIGN 7.6 specifies them as one mechanism. If it needs splitting, split at T4.1–T4.3 (the provider contract, testable standalone against the offline adapter) versus T4.4–T4.11 (Ask itself).
- **The parallel period doubles sync work.** Between Slice 2 and Slice 5 every organize write enqueues two outbox events and two deliveries. That is intentional (ADR step 2), but it means an embedding-sidecar outage produces a growing retry backlog while Khoj sync looks healthy. Watch outbox depth per event type during the parallel window.
- **Retrieval quality goes unmeasured, knowingly.** Dropping Khoj's cross-encoder rerank and all document chunking are accepted ADR simplifications, and under Decision B neither will be measured against Khoj before removal — there is no corpus and no baseline to measure with. The exposure is real and was accepted deliberately: if the new semantic channel is meaningfully worse at recall, it will surface in daily use rather than in a test, with no side-by-side available. What limits the blast radius: exact search (keyword, phrase, date, entity, filters) is untouched and is the precision channel; T3.4 holds a correctness floor; Decision D's counter makes the chunking trigger observable; and Ask's journal (Slice 4) makes a real comparison possible later, against live traffic, once documents exist. If semantic recall does disappoint in use, the ordered levers are: revisit chunking (ADR §1's trigger), then reranking (§1), then a different embedding model via a `reembed` run (DESIGN 8.5) — in that order, and each on evidence rather than suspicion.
- **The golden set can be quietly forgotten.** Decision B decouples DESIGN 15.2's golden set from Slice 5, which makes it easy to read as cancelled. It is not — Appendix A still lists it as a Release 1 acceptance item. T0.2's addendum must say so in the ADR itself, or the only record of the obligation will be this plan document.
