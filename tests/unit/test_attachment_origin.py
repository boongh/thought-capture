"""Which URLs the system will fetch an attachment from.

Ingestion streams a URL supplied by an external platform, and an authenticated
API caller can supply one too. Without this policy the service is a request
proxy: a URL or redirect pointing at ``169.254.169.254`` or ``127.0.0.1:5432``
makes it fetch something on its own network on the caller's behalf.
"""

from __future__ import annotations

import pytest

from tc_domain.attachment_origin import AttachmentOriginPolicy
from tc_domain.errors import AttachmentRejected

POLICY = AttachmentOriginPolicy()


def check(url: str) -> None:
    POLICY.check(url, filename="note.png")


# ---------------------------------------------------------------------------
# Permitted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.discordapp.com/attachments/1/2/note.png?ex=a&is=b&hm=sig",
        "https://media.discordapp.net/attachments/1/2/note.png",
    ],
)
def test_discord_cdn_urls_are_allowed(url: str) -> None:
    check(url)


# ---------------------------------------------------------------------------
# Refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "why"),
    [
        pytest.param("http://cdn.discordapp.com/a.png", "plaintext", id="http"),
        pytest.param("file:///etc/passwd", "local file", id="file-scheme"),
        pytest.param("https://evil.example.com/a.png", "unknown host", id="foreign-host"),
        pytest.param(
            "https://cdn.discordapp.com.evil.example/a.png",
            "suffix trick on an allowed name",
            id="lookalike-host",
        ),
    ],
)
def test_untrusted_origins_are_refused(url: str, why: str) -> None:
    with pytest.raises(AttachmentRejected):
        check(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data/",
        "https://127.0.0.1:5432/",
        "https://10.0.0.5/internal",
        "https://[::1]/internal",
    ],
)
def test_internal_targets_are_refused(url: str) -> None:
    """The shape an SSRF attempt actually takes."""
    with pytest.raises(AttachmentRejected, match="IP literal"):
        check(url)


def test_a_url_with_no_host_is_refused() -> None:
    with pytest.raises(AttachmentRejected):
        check("https:///no-host/a.png")


def test_rejection_names_the_host_but_never_the_signed_url() -> None:
    """The URL carries a signature that grants access to the object."""
    signed = "https://evil.example.com/a.png?hm=secret-signature"
    with pytest.raises(AttachmentRejected) as caught:
        check(signed)

    message = str(caught.value)
    assert "evil.example.com" in message
    assert "secret-signature" not in message


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------


def test_redirects_are_not_followed_by_default() -> None:
    """A followed redirect is a second request to a host nothing approved."""
    assert POLICY.allow_redirects is False


def test_the_allowlist_is_configurable_without_weakening_the_scheme_rule() -> None:
    """A deployment may add an origin; it may not opt out of https."""
    permissive = AttachmentOriginPolicy(allowed_hosts=frozenset({"files.example.org"}))

    permissive.check("https://files.example.org/a.png", filename="a.png")
    with pytest.raises(AttachmentRejected, match="https"):
        permissive.check("http://files.example.org/a.png", filename="a.png")
