"""Capture policies: who may capture, and what may be attached."""

from __future__ import annotations

from dataclasses import dataclass, field

from tc_domain.capture import AttachmentCandidate
from tc_domain.errors import AttachmentRejected, NotAllowlisted

# Executable and script types are refused outright. Release 1 does no scanning
# and no extraction, so anything that could be executed by a careless later
# double-click has no business in the archive (docs/DESIGN.md 12.2).
DEFAULT_BLOCKED_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "application/x-msdownload",
        "application/x-msdos-program",
        "application/x-executable",
        "application/x-sharedlib",
        "application/x-mach-binary",
        "application/vnd.microsoft.portable-executable",
        "application/x-bat",
        "application/x-sh",
        "application/x-shellscript",
        "application/javascript",
        "text/javascript",
    }
)

DEFAULT_BLOCKED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".exe",
        ".dll",
        ".scr",
        ".com",
        ".pif",
        ".bat",
        ".cmd",
        ".ps1",
        ".sh",
        ".msi",
        ".jar",
        ".vbs",
        ".js",
        ".lnk",
    }
)


@dataclass(frozen=True, slots=True)
class AttachmentPolicy:
    """Size and media-type limits applied before any bytes are copied."""

    max_bytes: int
    blocked_media_types: frozenset[str] = DEFAULT_BLOCKED_MEDIA_TYPES
    blocked_extensions: frozenset[str] = DEFAULT_BLOCKED_EXTENSIONS

    def validate(self, candidate: AttachmentCandidate) -> None:
        """Raise ``AttachmentRejected`` if the candidate is not permitted."""
        if candidate.size_bytes < 0:
            raise AttachmentRejected(candidate.filename, "negative size reported by source")
        if candidate.size_bytes > self.max_bytes:
            raise AttachmentRejected(
                candidate.filename,
                f"{candidate.size_bytes} bytes exceeds the {self.max_bytes} byte limit",
            )

        media_type = candidate.media_type.split(";", 1)[0].strip().lower()
        if media_type in self.blocked_media_types:
            raise AttachmentRejected(candidate.filename, f"media type {media_type!r} is blocked")

        # Compare the final suffix only. The stored object is named by hash, so
        # this guards what the archive contains, not how it is displayed.
        lowered = candidate.filename.lower()
        for extension in self.blocked_extensions:
            if lowered.endswith(extension):
                raise AttachmentRejected(candidate.filename, f"extension {extension!r} is blocked")


@dataclass(frozen=True, slots=True)
class CaptureAllowlist:
    """Exact-ID allowlist for the Discord capture surface.

    Release 1 supports a direct message to the bot or one configured private
    channel. Everything else is ignored and audited without storing its content
    (docs/DESIGN.md 4.1).
    """

    owner_user_id: int
    guild_id: int | None = None
    channel_id: int | None = None
    # Bot and webhook authors are never captured, regardless of ID.
    allow_bots: bool = field(default=False)

    def check(
        self,
        *,
        author_id: int,
        guild_id: int | None,
        channel_id: int | None,
        author_is_bot: bool,
    ) -> None:
        """Raise ``NotAllowlisted`` unless every configured constraint matches."""
        if author_is_bot and not self.allow_bots:
            raise NotAllowlisted("author is a bot or webhook")
        if author_id != self.owner_user_id:
            raise NotAllowlisted("author is not the workspace owner")

        if guild_id is None:
            # A direct message. Permitted: the owner check above is sufficient.
            return

        if self.guild_id is None:
            raise NotAllowlisted("guild capture is not configured; direct messages only")
        if guild_id != self.guild_id:
            raise NotAllowlisted("guild is not allowlisted")
        if self.channel_id is not None and channel_id != self.channel_id:
            raise NotAllowlisted("channel is not allowlisted")
