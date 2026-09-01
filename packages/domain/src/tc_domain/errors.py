"""Domain errors.

These describe rule violations, not transport or storage failures. Adapters
translate them into HTTP problem details or Discord replies at the boundary.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every rule violation raised by the domain."""


class CaptureRejected(DomainError):
    """The message is not eligible for capture and must not be acknowledged."""


class InvalidAllowlist(DomainError):
    """The allowlist configuration itself is unsafe or incomplete.

    Raised at construction rather than at check time, so a deployment that would
    capture a wider surface than intended fails at startup instead of quietly
    ingesting other people's messages.
    """


class NotAllowlisted(CaptureRejected):
    """The sender, guild, or channel is not on the allowlist.

    Content of a non-allowlisted message is never stored, and never included in
    this error, so that it cannot reach a log (docs/DESIGN.md 4.1, 12.2).
    """


class AttachmentRejected(CaptureRejected):
    """An attachment violated the configured size or media-type policy."""

    def __init__(self, filename: str, reason: str) -> None:
        # The filename is echoed for operator diagnosis; the bytes never are.
        super().__init__(f"attachment {filename!r} rejected: {reason}")
        self.filename = filename
        self.reason = reason


class AttachmentArchiveFailed(CaptureRejected):
    """The attachment could not be copied into content-addressed storage.

    Release 1 treats a message as one atomic capture, so this fails the whole
    capture rather than storing a thought with a missing attachment
    (docs/DESIGN.md 7.1).
    """


class InvalidCaptureCommand(CaptureRejected):
    """The command is internally inconsistent, e.g. a naive timestamp."""


class InvalidThresholds(DomainError):
    """A configured pair of thresholds is not a valid ordering (docs/DESIGN.md 6.4)."""
