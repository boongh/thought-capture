"""Ask domain types (docs/DESIGN.md 7.6, docs/adr/0003's "Ask proxy" amendment).

``enabled``/``degraded``/``strict_unsupported`` are independent, explicit
signals - never an empty ``answer`` a caller has to interpret. docs/DESIGN.md
7.5's "never return an empty success that implies no memory exists" applies
to Ask exactly as it does to hybrid search; this ADR amendment adds two more
reasons an answer can be legitimately absent: the operator has not opted into
Ask at all, and the caller asked for structured filters Ask cannot honor
(docs/DESIGN.md 7.6's "clear capability code... offers exact search results"
fallback).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from tc_domain.search import SearchPage


@dataclass(frozen=True, slots=True)
class AskReference:
    document_id: uuid.UUID
    title: str
    snippet: str


@dataclass(frozen=True, slots=True)
class AskChunk:
    """One increment of a streaming Ask answer (docs/DESIGN.md 7.6: "The
    gateway streams the answer").

    ``enabled``/``degraded``/``strict_unsupported`` are constant across every
    chunk of one call, so a consumer may read them from any chunk - including
    the first, without waiting for ``done``. Exactly one of the following is
    the reason a given chunk exists:

    - ``text_delta`` non-empty: incremental answer text.
    - ``references`` not ``None``: the deduped, resolved citation list, sent
      once.
    - ``fallback`` not ``None``: structured filters were requested but cannot
      be honored (see module docstring) - the exact-search substitute, sent
      once, with no LLM call made at all.

    Every stream ends with exactly one chunk carrying ``done=True``.
    """

    enabled: bool
    degraded: bool
    strict_unsupported: bool
    text_delta: str = ""
    references: tuple[AskReference, ...] | None = None
    fallback: SearchPage | None = None
    done: bool = False


@dataclass(frozen=True, slots=True)
class AskAnswer:
    """The fully-collected result of one Ask call - built by draining an
    ``AskChunk`` stream, for a caller (Discord) that has no use for real
    token-level streaming.

    Exactly one of these shapes is true at a time:

    - ``enabled=False``: Ask is not configured on this deployment
      (``Settings.ask_enabled`` is off, or Khoj's own chat-model
      configuration was never acknowledged - see ``Settings``). ``answer``,
      ``degraded``, and ``strict_unsupported`` carry no information.
    - ``enabled=True, strict_unsupported=True``: the request carried
      structured filters (kind/entity/date/phrase/etc.) Ask cannot honor -
      Khoj's evidence-injection path remains unverified (docs/DESIGN.md 7.6,
      docs/adr/0003 "Contract spike findings" item 3) - so no LLM call was
      made at all. ``fallback`` carries the equivalent exact-search results.
      ``answer`` is ``None``.
    - ``enabled=True, degraded=True``: Ask is configured but Khoj could not
      answer right now (unreachable, or Khoj itself has no chat model
      configured - docs/adr/0003 finding 4). ``answer`` is ``None``.
    - ``enabled=True, degraded=False, strict_unsupported=False``: a real
      answer, possibly with zero references if nothing indexed was relevant -
      that is still a normal, non-degraded outcome, the same way an empty
      exact-search page is.
    """

    enabled: bool
    degraded: bool
    strict_unsupported: bool
    answer: str | None
    references: tuple[AskReference, ...]
    fallback: SearchPage | None = None
