"""Entity naming and alias-merge policy.

Implements the naming half of docs/DESIGN.md 6.4: how a mention's name turns
into a lookup key, and how a similarity score turns into a merge decision.
Pure and stdlib-only, like the rest of the domain - the actual similarity
search is a PostgreSQL trigram query, which lives in ``tc_infrastructure``
because it is I/O, not policy.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from tc_domain.errors import InvalidThresholds


class EntityType(StrEnum):
    """Matches the ``entity_type`` CHECK constraint on ``entities``."""

    PERSON = "person"
    PROJECT = "project"
    PLACE = "place"
    ORGANIZATION = "organization"
    TOPIC = "topic"


class MatchDecision(StrEnum):
    """What a similarity score means for one candidate mention.

    - ``MERGE``: attach to the best-matching existing entity, adding an alias
      if the surface form is new. Exact name/alias matches always land here,
      because trigram ``similarity()`` of identical strings is 1.0.
    - ``AMBIGUOUS``: docs/DESIGN.md 6.4 forbids an automatic merge below the
      configured confidence threshold. The mention becomes its own entity
      rather than guessing - a wrong merge silently corrupts two people's
      history together, while a wrong split is just a duplicate a later
      owner-approved alias (docs/DESIGN.md 10) can fix. It remains visible
      to a review query because its normalized name is still close to the
      candidate it was not merged into.
    - ``NEW``: no existing entity is close enough to even flag; a plain new
      entity.
    """

    MERGE = "merge"
    AMBIGUOUS = "ambiguous"
    NEW = "new"


@dataclass(frozen=True, slots=True)
class EntityResolutionThresholds:
    """Confidence bounds for automatic alias merging (docs/DESIGN.md 6.4).

    ``merge_at`` and above: automatic merge. Below ``ambiguous_floor``: an
    unrelated new entity, not worth surfacing for review. In between: kept
    separate, but similar enough that a human might want to merge them by
    hand - the review queue's own definition (docs/DESIGN.md 6.4, "Ambiguous
    candidates remain separate and appear in a review queue").
    """

    merge_at: float = 0.82
    ambiguous_floor: float = 0.55

    def __post_init__(self) -> None:
        if not (0.0 <= self.ambiguous_floor <= self.merge_at <= 1.0):
            raise InvalidThresholds(
                f"thresholds must satisfy 0 <= ambiguous_floor ({self.ambiguous_floor}) "
                f"<= merge_at ({self.merge_at}) <= 1"
            )

    def classify(self, similarity: float | None) -> MatchDecision:
        """Turn a best-candidate similarity score into a merge decision.

        ``None`` means no candidate exists at all (an empty workspace, or the
        first entity of this type) - always ``NEW``.
        """
        if similarity is None:
            return MatchDecision.NEW
        if similarity >= self.merge_at:
            return MatchDecision.MERGE
        if similarity >= self.ambiguous_floor:
            return MatchDecision.AMBIGUOUS
        return MatchDecision.NEW


_WHITESPACE = re.compile(r"\s+")
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def normalize_entity_name(name: str) -> str:
    """Fold a display name to its lookup key.

    Unicode-casefolds (stronger than ``.lower()`` for non-ASCII scripts),
    strips accents so "Jose" and "José" collide, and collapses internal
    whitespace so formatting differences never split one entity in two.
    """
    folded = unicodedata.normalize("NFKD", name).casefold()
    without_marks = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return _WHITESPACE.sub(" ", without_marks).strip()


def document_stable_key(entity_type: EntityType, normalized_name: str) -> str:
    """The ``documents.stable_key`` an entity's document uses (docs/DESIGN.md 6.3, 8.3).

    ``normalized_name`` is expected already-normalized (``normalize_entity_name``),
    so this only has to make it filename- and Markdown-front-matter-safe: ASCII
    letters, digits, and hyphens, matching the design's own example
    (``project:thought-capture-ai``).

    ``_NON_SLUG`` strips every character outside ``a-z0-9``, so a name made
    entirely of non-Latin script (e.g. two different Japanese place names)
    or of stripped punctuation would otherwise both collapse to the same
    empty slug and collide under the ``(workspace_id, kind, stable_key)``
    unique key - a later entity silently overwriting an earlier one's
    document. A pure-ASCII name (the common case) keeps the readable slug
    unchanged; anything else gets a short content hash of the *full*
    normalized name appended (or used alone, if nothing ASCII survives),
    which makes two distinct normalized names collide only as likely as a
    SHA-256 collision.
    """
    slug = _NON_SLUG.sub("-", normalized_name).strip("-")
    if slug and normalized_name.isascii():
        return f"{entity_type}:{slug}"
    digest = hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()[:12]
    if slug:
        return f"{entity_type}:{slug}-{digest}"
    return f"{entity_type}:{digest}"
