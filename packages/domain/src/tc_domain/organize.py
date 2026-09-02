"""Domain types for the organize pipeline (docs/DESIGN.md 7.2).

Deliberately not the LLM's own Pydantic output schema
(``tc_application.organize_contract.OrganizationResult``): that schema exists
to validate an untrusted model reply and lives in ``tc_application``, which
domain code must not depend on (docs/DESIGN.md 5.3). Once a reply has been
validated, the application layer converts it into these plain, stdlib-only
types before handing it to the ``OrganizeWriter`` port, so the write side of
the pipeline - and its tests - never need Pydantic or an LLM response shape at
all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from tc_domain.capture import ThoughtId
from tc_domain.context import Inclusion, Signal
from tc_domain.entities import EntityType
from tc_domain.errors import DomainError


class OrganizeCoverageError(DomainError):
    """The model's output does not account for every thought in the window.

    Raised rather than silently accepted: docs/DESIGN.md 7.2 step 8 requires
    every input thought to be either cited or explicitly classified
    ``unorganized``, and a raised error here means the run fails before any
    derived row is written (docs/DESIGN.md 7.2 step 9 - never write a partial
    document set after validation failure).
    """


class WindowTooLargeError(DomainError):
    """The window's rendered text exceeds the organize pipeline's input bound.

    docs/DESIGN.md 14.1 calls for "deterministic chunking with overlap and
    final coverage validation" on a very large window; that splitter is not
    implemented yet (see the organize-pipeline slice's commit report). Until
    it is, an oversized window - an import, or a long stretch of missed
    catch-up windows merged into one - must fail loudly and cheaply, before
    a single token is sent to the provider, rather than silently paying for
    (and risking a timeout or truncation on) an unbounded prompt.
    """


@dataclass(frozen=True, slots=True)
class WindowThought:
    """The minimal shape of a raw thought the organize pipeline needs."""

    id: ThoughtId
    body: str
    client_local_date: str
    client_local_time: str


@dataclass(frozen=True, slots=True)
class EntityMentionWrite:
    entity_type: EntityType
    canonical_name: str
    surface_form: str
    confidence: float


@dataclass(frozen=True, slots=True)
class DocumentWrite:
    stable_key: str
    kind: str
    title: str
    body_markdown: str
    source_thought_ids: tuple[ThoughtId, ...]
    mentioned_entities: tuple[EntityMentionWrite, ...]
    change_summary: str


@dataclass(frozen=True, slots=True)
class ContextSelectionWrite:
    """A ``run_context_selections`` row still to be written (docs/DESIGN.md 7.3.5).

    Silently skipped by the writer for a ``stable_key`` with no existing
    ``documents`` row - the Tier 1 index can list an entity that has not
    produced a document yet (docs/DESIGN.md 7.3.1), and the table's foreign
    key has nothing to point at until one exists.
    """

    stable_key: str
    signals: tuple[Signal, ...]
    inclusion: Inclusion
    referenced_in_output: bool


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """The ``runs`` row fields a successful organize call finishes with.

    Passed into ``OrganizeWriter.write`` so the run's ``succeeded`` status
    transition lands in the *same* transaction as the documents, revisions,
    entities, and outbox event it describes - never as a separate write
    after the fact. A separate later write is exactly the crash window that
    let a completed-but-unmarked run go undetected by
    ``OrganizeScheduler``'s resume check, causing a retry to redo an
    already-committed window: a second run, a second digest document, and a
    second ``digest.ready`` event delivered to Discord for the same day.
    """

    model_provider: str | None
    model_id: str | None
    prompt_version: str
    input_tokens: int
    output_tokens: int
    context_recall: float | None
    context_degraded: bool


@dataclass(frozen=True, slots=True)
class OrganizeWriteRequest:
    documents: tuple[DocumentWrite, ...]
    context_selections: tuple[ContextSelectionWrite, ...]
    unorganized_thought_ids: tuple[ThoughtId, ...]


@dataclass(frozen=True, slots=True)
class OrganizeWriteResult:
    document_ids: dict[str, uuid.UUID]
    revision_ids: dict[str, uuid.UUID]
    digest_document_id: uuid.UUID | None


def validate_coverage(
    window_thought_ids: frozenset[ThoughtId],
    *,
    cited_thought_ids: frozenset[ThoughtId],
    unorganized_thought_ids: frozenset[ThoughtId],
) -> None:
    """Enforce docs/DESIGN.md 7.2 step 8.

    Every thought in the window must be accounted for, and nothing may be
    accounted for that was not actually in the window - a model that
    "cites" a thought ID from outside the window or workspace is exactly the
    kind of hallucination provenance exists to catch (docs/DESIGN.md 1.1:
    "Every generated claim is traceable to one or more raw thought IDs").
    """
    accounted_for = cited_thought_ids | unorganized_thought_ids
    missing = window_thought_ids - accounted_for
    if missing:
        raise OrganizeCoverageError(
            f"{len(missing)} thought(s) in the window were neither cited nor marked "
            f"unorganized: {sorted(missing)[:10]}"
        )

    foreign = accounted_for - window_thought_ids
    if foreign:
        raise OrganizeCoverageError(
            f"the model referenced {len(foreign)} thought ID(s) outside this window: "
            f"{sorted(foreign)[:10]}"
        )

    overlap = cited_thought_ids & unorganized_thought_ids
    if overlap:
        raise OrganizeCoverageError(
            f"{len(overlap)} thought(s) were both cited and marked unorganized: "
            f"{sorted(overlap)[:10]}"
        )
