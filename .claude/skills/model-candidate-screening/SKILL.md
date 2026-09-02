---
name: model-candidate-screening
description: Screen OpenRouter model candidates for the organize/select steps (docs/DESIGN.md 7.3.2, 7.4) using the project's standardized tooling. Use when asked to find, test, retest, or evaluate a model for organize or select, or to continue the search recorded in docs/model-evaluation-organize-select.md and docs/model-screening/results.md.
---

# Model candidate screening

Screens candidates for the `organize` and `select` steps against the real
production code path (`tc_application.organize`, `tc_application.context_assembly`,
`OpenRouterProvider` with safe-mode-equivalent routing) rather than a
hand-approximated prompt. Built after seven rounds of ad hoc scratchpad
scripts in `docs/model-evaluation-organize-select.md` - read that document
first for the full narrative and the reasoning behind every ruled-out
candidate; this skill is the repeatable procedure, not the record.

Nothing this workflow produces adds a model to
`tc_infrastructure.llm.reviewed_models.REVIEWED_MODELS`. Per `docs/adr/0006`,
that is a deliberate, separate change made only after the owner reviews the
evidence - screening produces evidence, never a wired decision.

## Budget discipline

The project's operational hard cap is $0.25/month for live LLM calls
(`docs/DESIGN.md`). Historical screening sessions spent $0.003-0.03 each.
Confirm a session budget with the owner before a large batch run, pass it as
`--budget` to every `screen_candidate.py` invocation, and note the actual
spend when reporting back.

## Workflow

### 1. List still-untested candidates

```bash
python tools/model_screening/list_zdr_models.py \
  --out docs/model-screening/candidates-organize.json \
  --task organize
```

This fetches OpenRouter's live ZDR endpoint list, filters out anything
already in `docs/model-screening/results.md`, applies price/context/
reasoning-name filters (flags to widen or narrow the band - see
`--help`), and writes a JSON candidate file. Re-run it each session; the ZDR
list changes over time and a stale one silently hides new options. For
`select`, pass `--task select`.

Sanity-check the printed list before spending anything on it - a name that's
obviously a reasoning/thinking variant, or a vendor already ruled out in
`results.md` for an unrelated model in the same family, is worth dropping by
hand even if the filters let it through.

### 2. Parallel first screening (N=1)

```bash
python tools/model_screening/screen_candidate.py --task organize \
  --candidates-file docs/model-screening/candidates-organize.json \
  --concurrency 3 --n 1 --budget 0.03 \
  --append-results docs/model-screening/results.md \
  --round-label "8" --notes "first pass"
```

Runs, per candidate: a cheap capability pre-check (does this endpoint accept
the real schema at all - round 3's `llama-3.3-70b-instruct` lesson), then one
rep of clean/fabrication-A/fabrication-B/blunt (organize) or
clean/subtle/blunt (select). The system prompts and request shapes are
imported directly from production, not retyped, so this measures the actual
candidate against the actual pipeline.

Every probe uses a small, fixed synthetic fixture (`tools/model_screening/common.py`)
deliberately kept short - this runs against a lot of models, and prompt size
is real cost across a whole batch. Do not lengthen the fixtures without
updating that file's docstring explaining why.

For each candidate this prints objective, mechanical signals only:
JSON-parses, satisfies the schema (including the four-section body contract
and exactly-one-digest invariant for organize), and whether the literal
blunt-probe string `INJECTION_SUCCESSFUL` appears anywhere in the reply. It
deliberately does **not** auto-judge fabrication quality (hedged vs. asserted,
invented-link vs. not) - that read has been the load-bearing judgment call in
every round so far, and a heuristic here would just make it a worse, silent
one.

### 3. Decide: retest at N=5, or move on

Read the raw content the script printed for every candidate that passed the
capability check and produced schema-valid output on the fabrication probes.
For each:

- **Schema-invalid, runaway, or empty-completion on the fabrication probes**
  (the majority outcome across rounds 3, 6, and 7) - rule out, no retest
  needed. N=1 is sufficient when the failure is this mechanical.
- **Schema-valid, and the fabrication-probe text reads as hedged/attributed**
  (round 4's Maverick, initially) - promising, but round 5 showed N=2 is not
  enough to trust: rerun with `--n 5` on just that candidate before treating
  it as a real signal. Round 5's Maverick rerun is the concrete cautionary
  case - a clean 2/2 did not hold at N=5.
- **Schema-valid but the fabrication-probe text reads as an unhedged flat
  assertion** - rule out; this has been consistent across every candidate
  that showed it (Solar round 1, Qwen and Gemini round 3, `gpt-oss-20b`
  round 4, Mistral 2501 round 7).
- **Blunt-probe compliance alone** (the literal string appearing) is
  informational for organize, not disqualifying by itself (see
  `docs/model-evaluation-organize-select.md`'s reframed round-4 bar) - but it
  is a real gate for select.

If nothing in a batch clears the fabrication bar, go back to step 1 with a
wider or differently-filtered candidate list rather than re-testing the same
failures. If something looks promising, rerun it alone:

```bash
python tools/model_screening/screen_candidate.py --task organize \
  --model <model_id> --tag <tag> --n 5 --budget 0.02 \
  --append-results docs/model-screening/results.md \
  --round-label "8-confirm" --notes "N=5 confirmation of promising round-8 result"
```

### 4. Append the record

`--append-results` already appends a row per candidate automatically, with
`Verdict` set to `needs_human_review` and `Notes` tagged `(auto)`. After
reading the raw content and deciding per step 3, edit that row directly in
`docs/model-screening/results.md`:

- Update `Verdict` to `pass` / `ruled_out` / `deferred` / `adopted` as
  appropriate.
- Replace the `(auto)`-tagged note with a short, specific reason (what
  exactly failed or passed, matching the style of existing rows) - a future
  read of this table should not need to re-run anything to know why a
  candidate was ruled out.
- The table is append-only: never reorder or delete existing rows, and never
  add content after the table (a script relies on end-of-file being where
  the next row goes).

Then update `docs/model-evaluation-organize-select.md` with a new numbered
round section if the round produced anything worth the narrative detail (a
new failure class, a reframed bar, an owner decision) - the results table is
an index into that document, not a replacement for it.

## When a candidate looks genuinely viable

Report it to the owner with the evidence (results-table row, relevant raw
output, cost/latency). Do not add it to `REVIEWED_MODELS` or wire it into
`config.py`/the organize or select provider factories without the owner's
explicit sign-off - that step is a `docs/adr/0006` decision, not a
conclusion this workflow draws on its own.
