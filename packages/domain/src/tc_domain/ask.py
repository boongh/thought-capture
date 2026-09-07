"""Ask domain types (docs/DESIGN.md 7.6, docs/adr/0010).

``AskAnswer.enabled`` and ``.degraded`` are independent, explicit signals -
never an empty ``answer`` a caller has to interpret. docs/DESIGN.md 7.5's
"never return an empty success that implies no memory exists" applies to Ask
exactly as it does to hybrid search, and docs/adr/0010 adds a second reason an
answer can be legitimately absent: the operator has not opted into Ask at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AskReference:
    document_id: uuid.UUID
    title: str
    snippet: str


@dataclass(frozen=True, slots=True)
class AskAnswer:
    """The result of one Ask call.

    Exactly one of these shapes is true at a time:

    - ``enabled=False``: Ask is not configured on this deployment
      (``Settings.ask_enabled`` is off). ``answer`` and ``degraded`` carry no
      information and are always ``None``/``False``.
    - ``enabled=True, degraded=True``: Ask is configured but Khoj could not
      answer right now (unreachable, or Khoj itself has no chat model
      configured - docs/adr/0003 finding 4). ``answer`` is ``None``.
    - ``enabled=True, degraded=False``: a real answer, possibly with zero
      references if nothing indexed was relevant - that is still a normal,
      non-degraded outcome, the same way an empty exact-search page is.
    """

    enabled: bool
    degraded: bool
    answer: str | None
    references: tuple[AskReference, ...]
