# ADR-0010: Retire Khoj — self-hosted embedding, pgvector search, and OpenRouter-routed Ask

- **Status:** Accepted
- **Date:** 2026-09-08
- **Design anchor:** `docs/DESIGN.md` 1, 5.2, 7.3.6, 7.5, 7.6, 8, 9, 9.2, 11, 12.1, 12.2, 13, 18, 19, 20
- **Supersedes:** `docs/adr/0003-postgresql-precision-retrieval-plus-khoj-semantic-ask.md`, in full — every decision in that ADR concerning semantic search, Ask/RAG, the Khoj container, `khoj-db`, and Khoj's credential/retention gates is replaced by this ADR. ADR-0003's PostgreSQL-precision-retrieval half is not touched; it remains accepted, described by `docs/DESIGN.md` 9.1.
- **First implemented in:** not yet — this ADR authorizes the design change; Slice 1 onward implement it.

## Context

`docs/DESIGN.md` 1 and ADR-0003 accepted a retrieval split: PostgreSQL owns
canonical facts and deterministic filters; self-hosted Khoj owns semantic
indexing, reranking, and Ask/RAG over exported Markdown. That split's
*framing* — first-party services own canonical/deterministic retrieval, a
replaceable component owns semantic retrieval and RAG — is not being
reconsidered here and is explicitly retained. What changes is who plays the
second role.

Living with the Khoj integration surfaced three problems, each reached
through direct experience building and reviewing it, not a preference stated
in the abstract:

1. **The integration cost was reverse-engineering an opaque external
   system, not "semantic search is hard."** ADR-0003's own history is the
   evidence: an undocumented `END_EVENT`-delimited streaming wire protocol
   that had to be captured live against a stub chat model
   (`tests/contract/khoj/stub_chat_model.py`) because the non-streaming shape
   was never verified beyond source-reading; a query-response cache bug
   (finding 3) that silently returns a deleted document's content for an
   identical query string; a non-root/log-path bug in Khoj's own CLI
   (`khoj/utils/cli.py`'s unexpanded `--log-file` default) that only a
   defensive non-root deployment exposed; malformed-response handling that
   needed its own remediation round (`HttpKhojClient.search`/`.chat` raising
   bare `KeyError`/`TypeError`/`ValueError`/`AttributeError` instead of
   `KhojUnavailableError`); and per-call conversation cleanup
   (`_delete_conversation`) needed only because Khoj keeps a server-side
   copy of every `/api/chat` call by default. Two independent review rounds
   on the Ask proxy alone (slice 20) found five P1s. None of this was
   semantic-retrieval difficulty; all of it was defending against a system
   this project does not control and cannot fix upstream without forking
   (explicitly out of scope, AGPL). Owning the code removes the category,
   not just today's instances of it.

2. **Ask's chat call required a second, ungoverned LLM credential.**
   ADR-0003 finding 4 and its "Ask proxy" amendment resolved Ask's privacy
   gap by adding `TC_KHOJ_OPENAI_BASE_URL`/`TC_KHOJ_OPENAI_API_KEY` — a
   provider credential Khoj itself calls, entirely outside
   `docs/adr/0006`'s safe-mode/reviewed-model/ZDR machinery, which governs
   every other model call this project makes. The eventual fix
   (`TC_ASK_PROVIDER_RETENTION_ACKNOWLEDGED`, `Settings
   .ask_provider_retention_acknowledged`,
   `packages/infrastructure/src/tc_infrastructure/config.py`) was a
   fail-fast attestation gate, not a structural close — it forces an
   operator to promise they reviewed whatever they pointed Khoj at; it
   cannot verify or enforce that review the way
   `_safe_mode_restricts_to_reviewed_models` enforces it for
   `model_organize`/`model_select`/`model_query_plan`. Routing Ask's chat
   call through the project's own `OpenRouterProvider` closes this
   structurally: one credential, one provider adapter, one policy surface,
   for every LLM call this project makes.

3. **Embedding is the wrong shape of dependency for a metered external
   call.** `docs/DESIGN.md` 9.2 already accepted local embedding as the
   default ("Khoj initially uses its local default search models to avoid
   sending embeddings externally"); 7.3.6 independently reached the same
   conclusion for organize-context embeddings ("Text embedding is
   inexpensive to host"). Embedding quality bar is low, compute cost is
   trivial, and paying a metered third-party API for something a small
   local model handles in milliseconds does not fit a system whose own
   design principle 9 is "local by default." This was never in tension with
   keeping Khoj — Khoj's own default embedding model already ran locally
   (ADR-0003 finding 5: `thenlper/gte-small`, CPU, ~30s cold start, no
   external calls). Retiring Khoj does not change this decision; it removes
   the container that happened to host it.

**Hardware constraint, which decides what stays on cloud.** The owner's
machine is an AMD Ryzen AI laptop with no dedicated GPU. `organize` and
`select` needed thirteen evaluation rounds (`docs/model-evaluation-organize-select.md`)
to find a cloud model (`x-ai/grok-4.3`) that reliably clears the
JSON-schema/reasoning quality bar `docs/DESIGN.md` 7.4/7.3.2 require. A
laptop-hostable local model would very likely regress on exactly the
failure modes that process screened for. **`organize`, `select`, and
`query_plan` are explicitly out of scope for this ADR and stay on
OpenRouter cloud, unchanged.** Only embedding moves local — the opposite
quality/cost profile from organize/select, per point 3 above.

**Python version constraint, which decides the runtime shape.** The main
monorepo is pinned to Python 3.14 (`docs/DESIGN.md` 5.2,
`pyproject.toml:94`). As of this ADR's writing, neither `torch` (latest
stable 2.14.0) nor `onnxruntime` (latest stable 1.29.0) ship `cp314`
wheels, and `sentence-transformers` hard-depends on `torch` with no way
around it. Local embedding therefore cannot run inside the main app's
Python environment today. It must run in its own sidecar service pinned to
an older Python — the same "separate runtime, own image" pattern Khoj
itself used (`docs/DESIGN.md` 5.2: "it must not share the first-party
Python 3.14 environment"), except first-party code instead of a vendored
image. **Revisit this pin once a stable `cp314` torch wheel ships**
(tracked upstream: `pytorch/pytorch#156856`); porting then should be a
rebuild-and-retest against the sidecar's existing test suite, not a
rewrite, since the model code itself does not change.

## Decision

### 1. Scope

In scope: semantic search (replacing `KhojPort.search` +
`SemanticHydrator`), Ask/RAG (replacing `KhojPort.chat`), the
indexing/sync pipeline that keeps them current, and full removal of the
Khoj container/adapter/credentials once the replacement is proven in a
later slice.

Explicitly out of scope, not silently deferred:

- `organize`/`select`/`query_plan` stay on OpenRouter cloud, per the
  hardware constraint above. Not touched by this ADR.
- No reranking model in v1. Khoj's cross-encoder rerank
  (`mixedbread-ai/mxbai-rerank-xsmall-v1`, ADR-0003 finding 5) is dropped;
  retrieval quality is pgvector cosine-similarity ranking alone, fused with
  exact search exactly as `docs/DESIGN.md` 7.5 already does today. Revisit
  only if retrieval quality actually proves insufficient in use
  (`docs/DESIGN.md` 15.2's golden-set recall/MRR targets are the trigger) —
  not built speculatively.
- No document chunking in v1 — one embedding vector per document's current
  revision, not per-passage chunk. This is a deliberate simplification
  versus Khoj, not an oversight: Khoj's internal chunking is exactly what
  caused the filename-dedup bug class documented in ADR-0003
  (`Search._semantic_results`'s "Known gap", `AskQuestion
  ._resolve_references`'s dedup workaround) — a whole class of bugs that
  stops being *possible*, not just handled, when there is one row per
  document. Revisit chunking only if a real quality problem (documents too
  long for one embedding to represent well) shows up in practice.
- A cloud embedding adapter is not built in this ADR — only designed for.
  `EmbeddingPort` (below) must make adding one later a single new adapter
  class, not a redesign.

### 2. Storage: pgvector in the first-party PostgreSQL, not a second server

Add the `pgvector` extension to the project's existing first-party
PostgreSQL, via a thin custom image layering `pgvector` onto the
already-pinned `postgres:18.6-trixie` base with a Dockerfile `RUN` step,
pinned by digest like every other image in this stack
(`docs/DESIGN.md` 13, 8.4's upgrade-by-digest policy). This is not a second
database server the way Khoj's `khoj-db` was.

**This explicitly reverses ADR-0003's original reasoning**, stated there as:
"Sharing one server would mean standardizing the shared server's base image
on a pgvector-capable build purely to satisfy Khoj's internal schema
requirement, coupling the first-party database's image choice to an
implementation detail of a system explicitly declared replaceable." That
reasoning applied to accepting an *external* system's fixed requirement —
Khoj's own reference deployment hard-required `pgvector/pgvector:pg15`, an
image this project did not control and could not rebuild to match its own
base-image policy. It does not apply here: pgvector is now a capability
this project owns outright, added to an image this project already builds
and pins by digest. There is no cross-image coupling concern to avoid,
because there is no second party's schema to protect against. Recording
this reversal explicitly, per this repository's convention (ADR-0003's own
"Index sync run-tracking scope" section) of stating a deviation rather than
silently rewriting prior text.

Vector index: **HNSW** (pgvector's modern index type) over IVFFlat — no
training step required, and a good recall/speed tradeoff at this project's
scale (single workspace, thousands not millions of rows, per
`docs/DESIGN.md` 7.3.6's cost envelope and `docs/DESIGN.md` 6's
single-workspace model).

### 3. One vector per document revision, not per chunk

Each *document* — not each revision — gets exactly one stored embedding
row, always holding whichever revision was most recently embedded
successfully. Concretely, `document_embeddings` is keyed by `document_id`
(primary key) and carries `revision_id` constrained by a **composite**
foreign key against `document_revisions (id, document_id)` — not a plain
single-column foreign key to `document_revisions(id)` alone, and not an
independent second foreign key to `documents(id)` either (both were an
early draft's shape). A single-column `revision_id` foreign key only
proves the revision exists somewhere; it does not prove it belongs to
*this* `document_id`, so a faulty sync writer could otherwise pair
document A with a revision that actually belongs to document B and
Postgres would accept it. The composite foreign key makes that
unrepresentable: `document_revisions (id, document_id)` is already
unique (its `id` primary key alone guarantees that), so the database
itself rejects any `document_embeddings` row whose `revision_id` isn't
one that actually belongs to its `document_id`. There is also no
independent `workspace_id` column at all — every read joins
`document_embeddings -> documents` for `workspace_id` rather than
trusting a second, separately-writable copy of it that could drift out of
sync with the row's actual owner (see `docs/DESIGN.md` 8.4 for the exact
schema, and 6.3 for the identical technique applied to
`documents.current_revision_id`, which had the analogous gap since
ADR-0004).

Currentness — "only the current revision is searchable" — is not a static
constraint a foreign key can express, because embedding sync is
asynchronous by design (`docs/DESIGN.md` 7.2 step 11's decoupled outbox,
unchanged from ADR-0003's shape) and therefore cannot be forced into the
same transaction as the revision write that supersedes a prior one. It is
enforced two ways instead: the sync writer's upsert only replaces a row
when the incoming revision's `revision_number` is strictly newer than
whatever is already stored (rejecting out-of-order, at-least-once outbox
redelivery of a superseded revision), and every semantic-search query
additionally filters on `document_embeddings.revision_id =
documents.current_revision_id`, so a document whose sync has not yet
caught up is simply absent from results rather than ever surfacing with
stale content attached to a current-looking hit. See `docs/DESIGN.md` 8.4
for the full mechanism.

Superseded revisions are not re-embedded or kept queryable through
semantic search, matching the design's former Khoj-era "only the current
revision is present in the live index" rule (`docs/DESIGN.md` 8.3 through
design version 1.8) — this ADR keeps that invariant, just enforced by the
write/read guarantees above instead of a Khoj upload/reindex cycle.

Record `embedding_model_id` on every stored row. The `runs` table already
has this column (`docs/DESIGN.md` 6.3, `packages/infrastructure/src/
tc_infrastructure/db/tables.py:108`) reused for exactly this purpose since
ADR-0003; this ADR extends its use to the new embedding table rather than
inventing a second place to track which model produced which vector, so a
future model or provider swap has a clean "which rows need re-embedding"
query (`docs/DESIGN.md` 6.3's existing `runs.kind = 'reembed'` enum member
already anticipates this — it was reserved-but-unused pending this exact
work, the same way `khoj_sync`/`partial` were reserved before ADR-0003's
"Index sync run-tracking scope" section activated them).

### 4. `EmbeddingPort`: provider-agnostic by construction

```python
class EmbeddingVector(BaseModel):
    values: tuple[float, ...]
    model_id: str
    dimensions: int

class EmbeddingPort(Protocol):
    async def embed(self, texts: tuple[str, ...]) -> tuple[EmbeddingVector, ...]: ...
```

Nothing above this port — chunking (there is none, per §1), storage
(§2/§3), or search (§5) — inspects which implementation produced a vector
beyond reading `model_id`/`dimensions` for the reembed-tracking query
above. A future `OpenRouterEmbeddingProvider` (`docs/DESIGN.md` 9.2's own
long-standing fallback: "If hardware performance is unacceptable,
configure its OpenAI-compatible embedding endpoint") implements this exact
protocol; adding it is a new adapter class registered at composition time,
not a change to `EmbeddingSyncLoop`, the search query, or Ask's evidence
assembly. This ADR does not build that adapter — only guarantees the seam
exists.

### 5. Sidecar: local embedding, Python 3.12, FastAPI

A new first-party sidecar service, own `pyproject.toml`/lockfile, own
Dockerfile, own pinned base image (Python 3.12 recommended — widest
current wheel coverage per the constraint above), FastAPI + uvicorn
matching the stack `apps/api` already uses. Fully separate from the main
monorepo's Python 3.14 toolchain, the same isolation `docs/DESIGN.md` 5.2
already required of Khoj ("it must not share the first-party Python 3.14
environment") — this ADR keeps that invariant for the new sidecar rather
than relaxing it.

Model (v1 recommendation): `thenlper/gte-small` via `sentence-transformers`
— the same model Khoj already used, and one this project already has
direct operational evidence about (ADR-0003 finding 5: CPU-only, local,
~30s cold start, no external calls). Reusing a known quantity avoids
re-litigating a model-choice research cycle inside this migration; it
remains swappable behind `EmbeddingPort` later.

### 6. Search: one step, not two

Old (Khoj era): `KhojPort.search` returns raw hits, keyed by filename;
`SemanticHydrator` re-resolves each hit back to a document, re-checks
workspace scoping, and re-applies filters — because Khoj's own filter
enforcement and chunked hits could not be trusted to already be correct
(ADR-0003's "Known gap": `Search._semantic_results` duplicate-hit issue;
the "Ask proxy" amendment's filename-dedup workaround).

New: `EmbeddingPort.embed(query)` produces a query vector;
`EmbeddingSearchPort.search` runs one SQL query directly against this
project's own `document_revisions`-joined pgvector column, with
workspace/date/entity/kind filters applied in the same `WHERE` clause as
the embedding's `<=>` distance ordering. One row per document (§3) makes
the dedup-by-filename class of bug structurally impossible, not handled —
there is no second system's chunking to dedupe against. `docs/DESIGN.md`
7.5's fusion step (RRF against the exact-search channel, `k=60`) is
unchanged; only what feeds the semantic channel changes.

### 7. Ask: OpenRouterProvider + `TC_MODEL_ASK`, not a Khoj credential

Ask's LLM call becomes a fourth caller of `OpenRouterProvider`
(`packages/infrastructure/src/tc_infrastructure/llm/openrouter.py`),
alongside `organize`, `select`, and `query_plan`. A new reviewed-model
slug, **`TC_MODEL_ASK`**, extends the same `Settings`/`REVIEWED_MODELS`/
safe-mode machinery `model_organize`/`model_select`/`model_query_plan`
already use — this ADR is the authorization for that extension, not a
description of code that already exists today. Concretely, Slice 1 must:

- add a `model_ask: str = ""` field to `Settings`
  (`packages/infrastructure/src/tc_infrastructure/config.py`), empty by
  default (no provider call) matching `model_organize`'s existing
  no-slug-means-offline convention;
- add `"model_ask": "ask"` to `_safe_mode_restricts_to_reviewed_models`'s
  `field_stages` mapping (`config.py:181-185` today), so an unreviewed or
  wrong-stage `TC_MODEL_ASK` fails process startup exactly like an
  unreviewed `model_organize` does now — not a runtime surprise on the
  first `/ask` call;
- add at least one entry to `REVIEWED_MODELS`
  (`tc_infrastructure.llm.reviewed_models`) whose `stages` includes
  `"ask"`, reviewed against this ADR §8's citation-contract prompt shape
  specifically, before `TC_ASK_ENABLED=true` can select a live model in
  safe mode.

Once wired, `TC_MODEL_ASK` is governed by exactly the same `zdr: true`,
`data_collection: deny`, `provider.only` routing, and served-model check
as every other reviewed model — no second credential, no second policy
surface. See `docs/DESIGN.md` 11 for the design-level statement of this
same contract.

**This removes `TC_ASK_PROVIDER_RETENTION_ACKNOWLEDGED`**
(`Settings.ask_provider_retention_acknowledged`,
`_ask_requires_provider_acknowledgment`) and its `env.example` entries
(`TC_KHOJ_OPENAI_BASE_URL`, `TC_KHOJ_OPENAI_API_KEY`,
`TC_ASK_PROVIDER_RETENTION_ACKNOWLEDGED`). That gate existed only because
Khoj's own outbound chat call was a route this project's code could not
see into or govern; once Ask is a normal `OpenRouterProvider` call,
`_safe_mode_restricts_to_reviewed_models` already covers it structurally,
and a second attestation gate saying "I promise I reviewed the thing I
can't verify" has nothing left to attest to. `TC_ASK_ENABLED`
(`Settings.ask_enabled`) is retained unchanged — it remains this project's
own independent feature gate, orthogonal to which provider serves the
model.

`docs/DESIGN.md` 7.6's streaming requirement, strict-filter capability-code
fallback, and per-call conversation-cleanup concerns are addressed
differently under this design, with one correction to how: **streaming is
not something `OpenRouterProvider` already does today.** `LLMProvider`
(`packages/domain/src/tc_domain/llm.py`) exposes only `complete()`, and
`OpenRouterProvider.complete()`
(`packages/infrastructure/src/tc_infrastructure/llm/openrouter.py:212`)
calls the OpenAI client without `stream=True` — `organize`/`select`/
`query_plan` are non-streaming today and stay that way. Ask needs a new
`LLMProvider.stream()` method, a new `LLMStreamChunk` value type, and a new
`LLMStreamInterrupted` error distinguishing a failure before any output
from one after partial output — the full contract is specified in
`docs/DESIGN.md` 7.6 (policy enforcement mid-stream, not only at the end;
usage journaling on both normal and interrupted completion; required test
coverage) and authorized here as part of this ADR, not assumed to already
exist. There is no server-side "conversation" left over to delete, since
OpenRouter's chat-completions call is stateless per request regardless of
streaming — the entire P1 category ADR-0003 needed a remediation round for
(buffered-not-streamed, no strict-filter fallback, no contract test for
chat, no conversation cleanup, no provider-retention gate) does not
reappear here because the new design's streaming contract is built and
tested to the same standard `complete()` already meets, rather than
wrapping a second, differently-shaped external chat API — but it is new
code, not reused code, and Slice 4 must build and test it before the API's
streamed-Ask contract can be satisfied.

### 8. Ask's evidence packet and citation-validation contract

Replacing Khoj's `/notes` chat mode with a raw `OpenRouterProvider`
completion removes Khoj's own reference-producing behavior along with it
— Khoj returned a structured `references.context` list the adapter only
had to parse (`docs/DESIGN.md`'s prior 7.6, ADR-0003's Ask-proxy
amendment). A plain chat-completion model returns free text; grounding and
citation validity have to be engineered back in explicitly rather than
inherited from the provider, or `docs/DESIGN.md` 1's "every generated
claim is traceable" requirement silently stops being enforced the moment
Khoj is removed.

**Evidence packet.** Before any model call, `EmbeddingSearchPort.search`'s
top-N hits are assembled into a numbered list (`AskEvidenceItem`: `index`,
`document_id`, `revision_id`, `title`, a bounded `snippet` of
`body_markdown` — not the full body). `index` is 1-based and stable only
for the duration of one request.

**Empty evidence never reaches the model.** Zero hits short-circuits to a
fixed, non-generated response before `TC_MODEL_ASK` is called at all —
the same structural "never invent a result" guarantee `docs/DESIGN.md`
7.6 states, but enforced by code rather than by trusting a canned
model-side reply the way Khoj's own no-notes-indexed behavior did.

**Citation contract.** The prompt requires every evidentiary claim to
carry an inline bracketed marker matching the cited item's `index` (e.g.
`[2]`), following the existing "quoted data, not instructions" framing
already used for organize prompts (`docs/DESIGN.md` 12.2). The provider
streams prose through the new `LLMProvider.stream()` contract (§7 above,
full specification in `docs/DESIGN.md` 7.6) — not through any existing
`organize`/`select` streaming, since neither of those streams today. After
the stream completes (or is interrupted — see §7's `LLMStreamInterrupted`
and `docs/DESIGN.md` 7.6's degrade behavior), a validation pass resolves
every `\[(\d+)\]` marker
against the evidence packet's `index` values: a marker resolving to a real
item becomes a validated `AskReference`; a marker that resolves to nothing
(a fabricated or out-of-range citation) is dropped from `references` and
left as inert text, never surfaced as a working citation, and never fails
the request outright.

**`citations_unverified` is keyed on the outcome (`references` empty),
never on whether the model bothered to emit any markers.** `citations_unverified
= (len(references) == 0)`, evaluated after validation, unconditionally.
An answer with markers that all fail to resolve and an answer with no
markers at all are the identical failure from a grounding standpoint —
zero verifiable claims — and get the identical flag. Gating the flag on
"markers were present" instead would let a model that skips the citation
instruction return as if it were normally, fully cited, quietly
reopening the untraceable-claim gap this section exists to close.

**Traceability closes through existing data, not a new mechanism.** A
validated `AskReference` names a `document_id`/`revision_id`; that
revision's `revision_sources` rows (`docs/DESIGN.md` 6.3, unchanged by
this ADR) already link it to the raw `thought_id`s that produced it —
resolving an Ask citation down to source thoughts reuses that table
directly.

See `docs/DESIGN.md` 7.6 for the full sequence, including the streamed
response shape and the mid-stream degrade case (evidence found, generation
failed).

## Architecture overview

```
Old:  organize writer -> outbox(khoj.sync_requested) -> KhojSyncLoop -> HttpKhojClient -> Khoj container
      Search/Ask       -> KhojPort.search/.chat       -> Khoj container -> SemanticHydrator (Postgres)

New:  organize writer -> outbox(embedding.sync_requested) -> EmbeddingSyncLoop -> EmbeddingPort (sidecar) -> pgvector row
      Search           -> EmbeddingPort.embed(query) -> EmbeddingSearchPort.search (direct pgvector query+join, one step)
      Ask               -> EmbeddingSearchPort.search -> prompt assembly -> OpenRouterProvider (TC_MODEL_ASK, streaming)
```

## Consequences

**Positive.** Removes an entire category of defensive engineering against
an opaque external system (§1's five named bug classes) — not because
those specific bugs are refixed better, but because there is no longer a
second system's undocumented protocol to reverse-engineer against.

**Positive.** Closes the Ask privacy gap structurally: one LLM credential,
one provider adapter (`OpenRouterProvider`), one policy surface
(`docs/adr/0006`), for organize, select, query_plan, and now ask.
`TC_ASK_PROVIDER_RETENTION_ACKNOWLEDGED` — an attestation gate that could
never verify what it gated — is removed rather than kept alongside a
now-redundant structural guarantee.

**Positive.** `docker-compose` loses `khoj`, `khoj-init`, and `khoj-db`
(three containers, ~5GB image) in favor of one small sidecar; the
first-party PostgreSQL image gains one `RUN` step. Net operational
footprint decreases.

**Negative.** The project now owns embedding-model lifecycle
(cold-start time, CPU cost, upgrade/reembed process) that was previously
Khoj's problem. Mitigated by reusing a model this project already has
direct operational evidence about (§5) and by the existing `reembed`
run-kind/`embedding_model_id` tracking (§3).

**Negative.** No reranking in v1 (§1 scope). Accepted as a deliberate
simplification pending evidence that pgvector cosine-similarity ranking
alone is insufficient against `docs/DESIGN.md` 15.2's golden-set targets.

**Negative.** A new sidecar service pinned to Python 3.12 is one more
moving part than embedding-inside-the-main-app would be, if that were
possible. Not avoidable today (Python 3.14/torch wheel gap, Context
above); tracked for retirement once `pytorch/pytorch#156856` ships a
`cp314` wheel — at which point the sidecar's own test suite should let
that be a rebuild, not a rewrite.

**Deferred.** Cloud embedding adapter (`EmbeddingPort` implementation
against OpenRouter's embeddings endpoint) — designed for (§4), not built.
Triggered only if local sidecar performance proves unacceptable on the
owner's hardware, mirroring `docs/DESIGN.md` 9.2's original Khoj-era
fallback language.

**Deferred.** Reranking and document chunking — both explicitly named
non-goals (§1), each with its own stated re-evaluation trigger rather than
an open-ended "maybe later."

## Verification

This ADR authorizes the design change; it does not itself add code. Slice
1 onward must each state, per this repository's implementation-behavior
requirements: which pieces of §2-§8 they implement, what tests exercise
them (`tests/unit`, `tests/integration` against a real pgvector-enabled
Postgres, a sidecar contract-test suite analogous to `tests/contract/khoj`
but against first-party code; an integration test proving the
`document_embeddings` composite foreign key (§3) rejects an
insert/update pairing a `document_id` with a `revision_id` belonging to a
different document; for `LLMProvider.stream()` (§7-8, full contract in
`docs/DESIGN.md` 7.6) — the streaming happy path, served-model rejection
on the first chunk with zero `delta` reaching the caller, a pre-content
transport failure raising plain `LLMError`, a post-content transport
failure raising `LLMStreamInterrupted` with the correct accumulated text,
and policy parity against `complete()`'s `provider_routing`; and — for §8's
citation contract specifically — citation-validation tests covering both a
fabricated/out-of-range marker, which must never become a returned
`AskReference`, and a marker-free answer, which must set
`citations_unverified: true` exactly like an all-invalid-markers answer
does), and confirmation via `scripts/check.ps1`/`scripts/check.sh`. Full
Khoj removal (container, adapter code, compose files, credentials) is
verified once the pgvector/sidecar/OpenRouter-Ask path passes
`docs/DESIGN.md` 15.2's golden-set recall/MRR thresholds at parity with or
better than the Khoj-era baseline — cutover is evidence-gated, not
simultaneous with this ADR's acceptance.

## Migration and rollback

**Forward path.** Slices land in the order: (1) pgvector-enabled Postgres
image + migration adding the embedding table (`document_embeddings`, with
its composite foreign keys) and HNSW index — this migration also widens
`runs.kind`'s CHECK constraint to add `'embedding_sync'` **without
removing `'khoj_sync'`**, since step 2 immediately below requires both to
be valid at once and a migration must never narrow a CHECK constraint in
the same step it widens one (`docs/DESIGN.md` 6.3 has the full rationale);
(2) embedding sidecar service + `EmbeddingPort`/`EmbeddingSyncLoop`,
running alongside Khoj sync, not replacing it yet; (3) `EmbeddingSearchPort`
wired into `Search._semantic_results` behind a flag, compared against
Khoj's results on the golden set; (4) `TC_MODEL_ASK` + `OpenRouterProvider`
Ask path, same comparison; (5) once parity is demonstrated, cut over
default routing, then remove Khoj (container, adapter, compose profile,
`TC_KHOJ_*`/`TC_ASK_PROVIDER_RETENTION_ACKNOWLEDGED` settings, `khoj_sync`
outbox/run machinery) — **this is the migration that finally drops
`'khoj_sync'` from the CHECK constraint**, since only at this point is it
certain no in-flight or historical row depends on it remaining valid.

**Rollback.** Each slice before step 5 is additive (new table, new
service, new flag) and can be reverted independently without touching the
still-live Khoj path. After step 5's cutover, rollback means re-enabling
the Khoj compose profile and flag — kept buildable, not deleted, until this
ADR's "Full Khoj removal" verification gate above is met.
