# ADR-0008: LLM call journal and derived-artifact rebuild

- **Status:** Accepted
- **Date:** 2026-08-31
- **Design anchor:** extends `docs/DESIGN.md` 6.3, 7.2, 11
- **First implemented in:** `migrations/versions/0004_derived_documents_and_provenance.py`

## Context

The project owner requires that switching models come with a way to reproduce
all derived artifacts. This is not in the accepted design, and it is the one
architectural addition in Phase 1.

It matters most because Release 1 starts on a cheap model. Committing to a weak
model is only safe if the decision is reversible — and "reversible" has to mean
something stronger than "we can run it again and hope".

The requirement splits into two capabilities that are frequently conflated, and
conflating them produces a promise the system cannot keep:

- **Replay** — rebuild derived documents from *stored model responses*, calling
  no provider. Deterministic. Reproduces a historical run exactly.
- **Regenerate** — re-run the pipeline over historical windows under a different
  model or prompt version. **Not** deterministic: LLM output is not reproducible
  across providers even at temperature 0. This is re-derivation, and it produces
  new revisions to be compared against the old.

Only the first is reproduction. Claiming otherwise would be claiming a guarantee
no provider offers.

## Decision

Persist every model call in an append-only `llm_calls` journal: `run_id`, step,
sequence, prompt and schema versions, model requested *and* model actually
served, provider, the provider's generation id, the exact rendered request
messages and parameters, the exact raw response, token usage, cost, latency, and
error. It is protected by the same trigger pattern as `thoughts` and
`document_revisions` — a journal that can be rewritten cannot reproduce anything.

`runs.kind` gains `replay` and `regenerate`; `runs.replay_of_run_id` links a
rebuild to the run it reproduces; `document_revisions.change_kind` gains
`regenerate`.

Three operations follow:

| Operation | Calls a provider | Deterministic | Purpose |
|---|---|---|---|
| `rebuild --from-journal` | No | Yes | Re-derive documents after a parsing, validation, or rendering change |
| `rebuild --regenerate --model X` | Yes | No | Re-derive history under a new model, then diff against the old |
| `rebuild --verify` | No | Yes | Re-render Markdown from stored revisions and assert `body_sha256` matches |

All three append. None mutate or delete, which follows from ADR-0004: a
regeneration under a new model can never destroy what the old model produced.

## Consequences

**Positive.** A model switch becomes a measurable decision rather than a leap:
regenerate history, diff the two revision sets, and judge. Prompt and parsing
bugs are fixable without paying for inference again. Every run is auditable
against the exact bytes the provider returned, which is what makes
`docs/DESIGN.md` 15.3's regression-diff evaluation possible.

**Negative — and this one is significant.** `llm_calls` contains raw personal
content, because organize prompts embed thought bodies verbatim. It is therefore
as sensitive as `thoughts` itself. It lives in the canonical database beside
them, is never written to application logs, is never exported to Khoj, and is
covered by backup encryption and portable export. Any future retention or
deletion policy must treat it as personal data, not as diagnostics.

**Negative.** Journal size grows with run count and prompt size. The design's
cost envelope bounds a run's input to ~13k tokens (`docs/DESIGN.md` 7.3.6), so
one journal row is tens of kilobytes and a year of daily runs is single-digit
megabytes. Pruning, if ever needed, forfeits replay for the pruned period and is
an owner decision.

**Deferred.** The rebuild commands are not implemented by this ADR's first
commit; the schema that makes them possible is. They land in the rebuild slice.

## Verification

`tests/integration/test_derived_schema.py` asserts that `llm_calls` rejects
`UPDATE`, that the `replay` and `regenerate` run kinds are accepted, and that a
replay run can link to the run it reproduces. The rebuild commands bring their
own tests, including a byte-identical journal replay and a regeneration that
leaves the original revisions reachable.
