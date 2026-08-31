"""Which URLs the system is willing to fetch an attachment from.

Ingestion streams a URL supplied by an external platform, and an authenticated
API caller can supply one too. Without a policy the service is a request proxy:
a redirect to ``http://169.254.169.254/`` or ``http://127.0.0.1:5432/`` makes it
fetch something on its own network on the caller's behalf.

The rule is an allowlist, not a denylist. Blocking known-bad addresses fails
open on anything not thought of; permitting only the CDNs attachments actually
come from fails closed.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from tc_domain.errors import AttachmentRejected

# Discord's attachment CDNs. Exact hosts and their subdomains only.
DEFAULT_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {"cdn.discordapp.com", "media.discordapp.net", "cdn.discord.com"}
)


@dataclass(frozen=True, slots=True)
class AttachmentOriginPolicy:
    """Decides whether a URL may be fetched."""

    allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS
    # Redirects are refused outright rather than followed and re-checked. A
    # followed redirect is a second, unvetted request, and the attachment CDNs
    # serve their objects directly.
    allow_redirects: bool = False

    def check(self, url: str, *, filename: str) -> None:
        """Raise ``AttachmentRejected`` unless the URL is permitted."""
        parts = urlsplit(url)

        if parts.scheme != "https":
            raise AttachmentRejected(filename, f"{parts.scheme or 'missing'} is not https")

        host = (parts.hostname or "").lower()
        if not host:
            raise AttachmentRejected(filename, "the attachment URL has no host")

        if _is_ip_literal(host):
            # A literal address can never be an allowlisted CDN, and is the
            # usual shape of an attempt to reach something internal.
            raise AttachmentRejected(filename, "the attachment URL is an IP literal")

        if not self._host_allowed(host):
            # The host is named, not echoed with the URL: the URL carries a
            # signature that grants access to the object.
            raise AttachmentRejected(filename, f"host {host!r} is not an allowed attachment origin")

    def _host_allowed(self, host: str) -> bool:
        return any(
            host == allowed or host.endswith(f".{allowed}") for allowed in self.allowed_hosts
        )


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True
