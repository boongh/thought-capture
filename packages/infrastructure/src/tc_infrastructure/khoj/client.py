"""Khoj HTTP client, implementing ``KhojPort`` (docs/adr/0003).

Endpoints and shapes confirmed against a real running
``ghcr.io/khoj-ai/khoj:2.0.0-beta.28`` container (docs/adr/0003's contract
spike), not inferred from documentation alone. No auth header is sent: Khoj
runs ``--anonymous-mode``, loopback-only (docs/adr/0003's auth-mode
decision) - the base URL is the only configuration this adapter needs.
"""

from __future__ import annotations

import httpx

from tc_domain.khoj_ports import KhojIndexFile, KhojSearchResult, KhojUnavailableError

REQUEST_TIMEOUT_SECONDS = 30.0

# Every file this adapter ever sends is our own generated Markdown export
# (docs/DESIGN.md 8.3); restricting both index and search to this type keeps
# results scoped to content we actually control the shape of.
CONTENT_TYPE = "markdown"


class HttpKhojClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")

    async def index(self, files: tuple[KhojIndexFile, ...]) -> None:
        if not files:
            return
        upload = [("files", (f.filename, f.content, "text/markdown")) for f in files]
        try:
            response = await self._http.put(
                f"{self._base_url}/api/content",
                params={"t": CONTENT_TYPE},
                files=upload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise KhojUnavailableError(f"Khoj index upload failed: {exc}") from exc

    async def delete(self, filenames: tuple[str, ...]) -> None:
        if not filenames:
            return
        try:
            response = await self._http.request(
                "DELETE",
                f"{self._base_url}/api/content/files",
                json={"files": list(filenames)},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise KhojUnavailableError(f"Khoj delete failed: {exc}") from exc

    async def search(self, q: str, *, limit: int = 5) -> tuple[KhojSearchResult, ...]:
        try:
            response = await self._http.get(
                f"{self._base_url}/api/search",
                params={"q": q, "n": limit, "t": CONTENT_TYPE},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise KhojUnavailableError(f"Khoj search failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise KhojUnavailableError(f"Khoj search returned a non-JSON response: {exc}") from exc

        try:
            return tuple(
                KhojSearchResult(
                    entry=item["entry"],
                    score=float(item["score"]),
                    filename=item.get("additional", {}).get("file", ""),
                )
                for item in payload
            )
        except (KeyError, TypeError, ValueError) as exc:
            # A malformed shape (missing "entry"/"score", a non-list body, a
            # non-numeric score) is exactly as unusable to the caller as an
            # unreachable Khoj - both must degrade the same way (docs/DESIGN.md
            # 7.5), so this is not allowed to surface as a bare KeyError/
            # TypeError that a caller's `except KhojUnavailableError` would miss.
            raise KhojUnavailableError(f"Khoj search returned an unexpected shape: {exc}") from exc
