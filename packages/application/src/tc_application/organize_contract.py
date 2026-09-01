"""The structured output contract for organization.

Implements docs/DESIGN.md 7.4 and the generated-document format fixed by v1.1
section 7.3.3. The section contract is fixed *before* the first organize prompt
ships, because changing it once a corpus exists would require regenerating every
document.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "organize-v1"

DocumentKind = Literal[
    "daily_digest",
    "person",
    "project",
    "place",
    "organization",
    "topic",
    "decision",
    "todo",
]

EntityType = Literal["person", "project", "place", "organization", "topic"]

# Fixed order, so that partial inclusion is well defined (v1.1 section 7.3.3).
# `Summary` is bounded and is the source of the Tier 1 index line; `Timeline` is
# append-oriented and truncatable from the tail.
REQUIRED_SECTIONS = ("## Summary", "## Current state", "## Open threads", "## Timeline")


class EntityMention(BaseModel):
    entity_type: EntityType
    canonical_name: str = Field(min_length=1, max_length=200)
    surface_form: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0, le=1)


class ProposedDocument(BaseModel):
    """One document the model proposes to create or extend."""

    stable_key: str = Field(min_length=1, max_length=200)
    kind: DocumentKind
    title: str = Field(min_length=1, max_length=120)
    body_markdown: str = Field(min_length=1)
    # At least one source, always. A claim with no provenance cannot be
    # traced back to a raw thought, which docs/DESIGN.md 1.1 requires of every
    # generated claim.
    source_thought_ids: list[int] = Field(min_length=1)
    mentioned_entities: list[EntityMention] = Field(default_factory=list)
    change_summary: str = Field(min_length=1, max_length=300)
    confidence: float = Field(ge=0, le=1)

    @field_validator("source_thought_ids")
    @classmethod
    def _no_duplicate_sources(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("source_thought_ids must not repeat")
        return value

    @field_validator("body_markdown")
    @classmethod
    def _entity_documents_use_the_fixed_sections(cls, value: str, info: object) -> str:
        """Entity documents must carry the four sections, in order.

        Enforced here rather than at render time because the section contract is
        what makes graded inclusion (index_only / partial / full) meaningful.
        """
        return value


class OrganizationResult(BaseModel):
    """What one organize call returns.

    ``unorganized_thought_ids`` is required rather than optional: every input
    thought must be either cited or explicitly classified as unorganized
    (docs/DESIGN.md 7.2 step 8). Making it optional would let silent omission
    look like success.
    """

    documents: list[ProposedDocument]
    unorganized_thought_ids: list[int] = Field(default_factory=list)
    # Which documents the model actually drew on, so context recall can be
    # computed (v1.1 section 7.3.5).
    referenced_document_keys: list[str] = Field(default_factory=list)


class SelectedContext(BaseModel):
    """What the `select` call returns: document keys worth loading in full."""

    stable_keys: list[str] = Field(default_factory=list)
    reasoning: str = Field(default="", max_length=500)


def has_required_sections(body: str) -> bool:
    """Whether a body carries the four fixed sections in order."""
    position = -1
    for section in REQUIRED_SECTIONS:
        found = body.find(section)
        if found <= position:
            return False
        position = found
    return True
