"""Context assembly for organization (docs/DESIGN.md 7.3).

Pure policy: given the workspace's complete entity-document index and the raw
text of the window being organized, decide which documents' bodies are worth
sending in full. The actual reads (documents, entity mentions, aliases) are
PostgreSQL queries and live in ``tc_infrastructure``; the LLM ``select`` call
is a network round trip and lives in ``tc_application``. This module has
neither, so the deterministic half of assembly is testable without either.

Two invariants from docs/DESIGN.md 7.3.4 are the shape of this module:

- **The index is complete** (invariant 1): every ``Tier1Row`` the caller
  passes in is assumed to already be "every entity document in the
  workspace" - nothing here filters or samples it.
- **Selection is additive** (invariant 2): each signal function returns a set
  of stable keys to *add*; nothing in this module ever removes a key another
  signal already selected.

v1 scope, documented rather than silently dropped (see docs/adr, this
package's callers, and the organize-pipeline slice's commit report): only
``alias``, ``recency``, and ``open_thread`` are implemented here as
deterministic signals. ``cooccurrence`` and the embedding signal
(docs/DESIGN.md 7.3.6, explicitly a "second wave" even in the design itself)
are not - a workspace with few enough entities to fit the whole index in a
prompt (docs/DESIGN.md 7.3.1) does not yet need them to avoid fragmentation,
and adding an unproven signal before ``context_recall`` has a baseline is
exactly what docs/DESIGN.md 7.3.6 warns against ("Enabling every signal at
once makes an underperforming or redundant signal impossible to identify").

Inclusion is the design's full three-way grade (docs/DESIGN.md 7.3.3):
``full``, ``partial``, or ``index_only``. ``assemble`` decides the grade from
each document's ``body_tokens`` (an approximate count the index-building
caller supplies, since counting real tokens needs the whole body, which this
module never sees) against two budgets from docs/DESIGN.md 7.3.6: a per-
document ``max_body_tokens`` ceiling, and an aggregate
``max_selected_body_tokens`` (<= 8k tokens) across everything selected. What
this module does *not* do is the actual text truncation a ``partial`` grade
implies (Summary/Current state/Open threads/timeline-tail, per 7.3.3) -
that needs the real Markdown body and the fixed section contract, which only
exist once a document is actually fetched, downstream in
``tc_infrastructure``/``tc_application``. This module only ever decides
*how much* of a document is worth fetching.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Literal

from tc_domain.entities import normalize_entity_name

Inclusion = Literal["full", "partial", "index_only"]

# Every signal that can add a document to the selected set. Mirrors the
# `signals text[]` column on `run_context_selections` (docs/DESIGN.md 7.3.5).
Signal = Literal["alias", "recency", "open_thread", "selector"]


@dataclass(frozen=True, slots=True)
class ContextAssemblyConfig:
    """Workspace-scoped, versioned with the prompt (docs/DESIGN.md 7.3.6)."""

    max_selected_documents: int = 12
    recency_days: float = 3.0
    # docs/DESIGN.md 7.3.3: a document over this size is never sent `full`,
    # only `partial`. docs/DESIGN.md 7.3.6: the cost/privacy envelope caps
    # everything sent as `full` or `partial` combined at 8k tokens - an
    # immutable, ever-growing document set can otherwise blow both budgets
    # even while staying within `max_selected_documents`.
    max_body_tokens: int = 2000
    max_selected_body_tokens: int = 8000


DEFAULT_CONTEXT_ASSEMBLY_CONFIG = ContextAssemblyConfig()


@dataclass(frozen=True, slots=True)
class Tier1Row:
    """One line of the complete entity-document index (docs/DESIGN.md 7.3.1)."""

    stable_key: str
    entity_type: str
    canonical_name: str
    aliases: tuple[str, ...]
    summary: str
    last_mentioned_at: dt.datetime | None
    open_thread_count: int
    # Approximate token count of the document's *full* current body (not just
    # `summary`) - the budgeting input `assemble` needs to decide `full` vs
    # `partial` vs `index_only`. The index-building caller computes this from
    # the real body, since this module never receives body text itself.
    body_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Selection:
    """One document's assembled inclusion, and why."""

    stable_key: str
    inclusion: Inclusion
    signals: tuple[Signal, ...]


def alias_signal(window_text: str, index: tuple[Tier1Row, ...]) -> frozenset[str]:
    """Entities named or misspelled-but-recognizable in the window text.

    A word-boundary check against the normalized window rather than a second
    trigram query: the index is already in memory by the time this runs, and
    this signal exists to catch exact or near-exact naming, not fuzzy recall
    - fuzzy recall is what ``selector`` and, later, the embedding signal are
    for. Matching is anchored to word boundaries (not a bare substring test)
    so a short name like "Ann" does not match inside an unrelated word like
    "annual" and pull an unrelated private document into the prompt.
    """
    haystack = normalize_entity_name(window_text)
    matched = set()
    for row in index:
        names = (row.canonical_name, *row.aliases)
        for name in names:
            if not name.strip():
                continue
            normalized_name = normalize_entity_name(name)
            if not normalized_name:
                continue
            pattern = rf"(?<!\w){re.escape(normalized_name)}(?!\w)"
            if re.search(pattern, haystack):
                matched.add(row.stable_key)
                break
    return frozenset(matched)


def recency_signal(
    index: tuple[Tier1Row, ...], *, now: dt.datetime, recency_days: float
) -> frozenset[str]:
    """Entities mentioned recently enough that an unnamed continuation likely means them.

    ``recency_days`` approximates docs/DESIGN.md 7.3.2's "last
    ``context.recency_windows`` windows": a capture window is very close to
    one calendar day (docs/DESIGN.md 4.2), so a day count is the same signal
    without needing to look up how many *windows*, specifically, elapsed.
    """
    if now.tzinfo is None:
        raise ValueError("recency_signal needs a timezone-aware `now`")
    cutoff = now - dt.timedelta(days=recency_days)
    return frozenset(
        row.stable_key
        for row in index
        if row.last_mentioned_at is not None and row.last_mentioned_at >= cutoff
    )


def open_thread_signal(index: tuple[Tier1Row, ...]) -> frozenset[str]:
    """Entities with an unresolved todo or decision attached.

    Standing context, included whether or not the window names them
    (docs/DESIGN.md 7.3.2's ``open_thread`` row).
    """
    return frozenset(row.stable_key for row in index if row.open_thread_count > 0)


def assemble(
    index: tuple[Tier1Row, ...],
    *,
    deterministic: dict[Signal, frozenset[str]],
    selector_keys: frozenset[str] = frozenset(),
    selector_degraded: bool = False,
    config: ContextAssemblyConfig = DEFAULT_CONTEXT_ASSEMBLY_CONFIG,
) -> tuple[tuple[Selection, ...], bool]:
    """Combine every signal, apply both budgets, and record why each document was included.

    Returns the per-document selection (covering the *entire* index, so a
    caller can persist ``run_context_selections`` for every document in one
    pass - invariant 6) and whether the run should be marked degraded.

    Two budgets apply, in order, to the documents any signal picked:

    1. **Count**: at most ``config.max_selected_documents`` are considered at
       all. The *deterministic* signals win first - alias, recency, and
       open-thread hits are precisely what invariant 2 says the selector can
       never override - and any excess (deterministic overflow, or a
       selector-only pick beyond the cap) falls back to ``index_only``.
    2. **Tokens**: within that set, a document over ``config.max_body_tokens``
       can never be ``full`` - only ``partial`` (docs/DESIGN.md 7.3.3). Then,
       walked in the same deterministic-first, stable order, each document's
       token cost accumulates against ``config.max_selected_body_tokens``
       (docs/DESIGN.md 7.3.6's <= 8k-token cap on everything sent as body
       content); once a document would push the running total over that cap
       it is downgraded - ``full`` to ``partial``, and ``partial`` (or a
       ``full`` that is still too large even alone) to ``index_only`` -
       rather than the budget being silently exceeded. An immutable,
       ever-growing document set is what makes this necessary: staying under
       ``max_selected_documents`` does not bound token cost.

    This module never truncates a body's actual text - it only decides how
    much of a document is worth fetching. The real Markdown truncation a
    ``partial`` grade implies happens downstream, where the body exists.
    """
    signals_by_key: dict[str, set[Signal]] = {}
    for signal_name, keys in deterministic.items():
        for key in keys:
            signals_by_key.setdefault(key, set()).add(signal_name)
    for key in selector_keys:
        signals_by_key.setdefault(key, set()).add("selector")

    deterministic_keys = {key for keys in deterministic.values() for key in keys}
    # Deterministic hits first, in a stable order; selector-only picks after.
    # Sorting by stable_key makes which documents get truncated under a tight
    # budget reproducible across two runs of the same window, which matters
    # for docs/DESIGN.md 15.3's regression diffs.
    ordered = sorted(signals_by_key, key=lambda key: (key not in deterministic_keys, key))
    candidates = ordered[: config.max_selected_documents]

    body_tokens = {row.stable_key: row.body_tokens for row in index}
    inclusion_by_key: dict[str, Inclusion] = {}
    spent_tokens = 0
    for key in candidates:
        tokens = body_tokens.get(key, 0)
        if (
            tokens <= config.max_body_tokens
            and spent_tokens + tokens <= config.max_selected_body_tokens
        ):
            inclusion_by_key[key] = "full"
            spent_tokens += tokens
        elif spent_tokens + min(tokens, config.max_body_tokens) <= config.max_selected_body_tokens:
            inclusion_by_key[key] = "partial"
            spent_tokens += min(tokens, config.max_body_tokens)
        else:
            inclusion_by_key[key] = "index_only"

    selections = tuple(
        Selection(
            stable_key=row.stable_key,
            inclusion=inclusion_by_key.get(row.stable_key, "index_only"),
            signals=tuple(sorted(signals_by_key.get(row.stable_key, ()))),
        )
        for row in index
    )
    return selections, selector_degraded
