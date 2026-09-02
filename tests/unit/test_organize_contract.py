"""The organize structured-output contract (docs/DESIGN.md 7.3.3, 7.4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tc_application.organize_contract import OrganizationResult, has_required_sections

SECTIONED_BODY = (
    "## Summary\n\nx\n\n## Current state\n\nx\n\n## Open threads\n\nx\n\n## Timeline\n\nx"
)


def _document(
    *,
    stable_key: str = "person:jane",
    kind: str = "person",
    body_markdown: str = SECTIONED_BODY,
) -> dict[str, object]:
    return {
        "stable_key": stable_key,
        "kind": kind,
        "title": "Jane",
        "body_markdown": body_markdown,
        "source_thought_ids": [1],
        "mentioned_entities": [],
        "change_summary": "created",
        "confidence": 0.9,
    }


def _digest(
    *, stable_key: str = "daily_digest:x", body_markdown: str = "## Summary\n\nx"
) -> dict[str, object]:
    return _document(stable_key=stable_key, kind="daily_digest", body_markdown=body_markdown)


class TestHasRequiredSections:
    def test_accepts_the_four_sections_in_order(self) -> None:
        assert has_required_sections(SECTIONED_BODY) is True

    def test_rejects_a_missing_section(self) -> None:
        assert has_required_sections("## Summary\n\nx\n\n## Timeline\n\nx") is False

    def test_rejects_sections_out_of_order(self) -> None:
        swapped = (
            "## Current state\n\nx\n\n## Summary\n\nx\n\n## Open threads\n\nx\n\n## Timeline\n\nx"
        )
        assert has_required_sections(swapped) is False


class TestProposedDocumentSections:
    def test_a_non_digest_document_without_sections_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="fixed section order"):
            OrganizationResult.model_validate(
                {
                    "documents": [
                        _document(body_markdown="just some prose, no sections"),
                        _digest(),
                    ],
                    "unorganized_thought_ids": [],
                    "referenced_document_keys": [],
                }
            )

    def test_a_non_digest_document_with_the_sections_is_accepted(self) -> None:
        result = OrganizationResult.model_validate(
            {
                "documents": [_document(), _digest()],
                "unorganized_thought_ids": [],
                "referenced_document_keys": [],
            }
        )
        assert len(result.documents) == 2

    def test_a_digest_without_the_four_sections_is_accepted(self) -> None:
        """The digest is exempt - it is never re-fetched or partially included."""
        result = OrganizationResult.model_validate(
            {
                "documents": [_digest(body_markdown="## Summary\n\njust a summary")],
                "unorganized_thought_ids": [],
                "referenced_document_keys": [],
            }
        )
        assert result.documents[0].kind == "daily_digest"


class TestExactlyOneDailyDigest:
    def test_zero_digests_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exactly one daily_digest"):
            OrganizationResult.model_validate(
                {
                    "documents": [_document()],
                    "unorganized_thought_ids": [],
                    "referenced_document_keys": [],
                }
            )

    def test_two_digests_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exactly one daily_digest"):
            OrganizationResult.model_validate(
                {
                    "documents": [
                        _digest(stable_key="daily_digest:a"),
                        _digest(stable_key="daily_digest:b"),
                    ],
                    "unorganized_thought_ids": [],
                    "referenced_document_keys": [],
                }
            )

    def test_exactly_one_digest_is_accepted(self) -> None:
        result = OrganizationResult.model_validate(
            {
                "documents": [_digest()],
                "unorganized_thought_ids": [],
                "referenced_document_keys": [],
            }
        )
        assert len(result.documents) == 1
