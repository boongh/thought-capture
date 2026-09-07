"""Khoj HTTP client, implementing ``KhojPort`` (docs/adr/0003).

Endpoints and shapes confirmed against a real running
``ghcr.io/khoj-ai/khoj:2.0.0-beta.28`` container (docs/adr/0003's contract
spike), not inferred from documentation alone. No auth header is sent: Khoj
runs ``--anonymous-mode``, loopback-only (docs/adr/0003's auth-mode
decision) - the base URL is the only configuration this adapter needs.
"""

from __future__ import annotations

import httpx

from tc_domain.khoj_ports import (
    KhojChatReference,
    KhojChatResult,
    KhojIndexFile,
    KhojSearchResult,
    KhojUnavailableError,
)

REQUEST_TIMEOUT_SECONDS = 30.0
# /api/chat can involve a live LLM call once Khoj has a chat model configured
# (docs/adr/0003 finding 4 and its "Ask proxy" amendment); generous relative
# to the search/content endpoints, which never call out to a model at all.
CHAT_REQUEST_TIMEOUT_SECONDS = 60.0

# Every file this adapter ever sends is our own generated Markdown export
# (docs/DESIGN.md 8.3); restricting both index and search to this type keeps
# results scoped to content we actually control the shape of.
CONTENT_TYPE = "markdown"

# Forces Khoj's "notes-only" retrieval mode (docs/DESIGN.md 7.6): no online
# search, no code execution, no agentic tool loop - both of the former are
# undeployed anyway (docs/adr/0003 decision 5), and this makes that explicit
# at the request level too, not just by omission of those services.
_NOTES_MODE_PREFIX = "/notes"


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

    async def chat(self, question: str, *, limit: int = 5) -> KhojChatResult:
        # Request shape (q/n/stream/create_new) verified against
        # `ChatRequestBody` and the non-streaming branch of `POST /api/chat`
        # in khoj's own source at the exact pinned tag (docs/adr/0003's "Ask
        # proxy" amendment) - `stream=False` returns a plain JSON body
        # instead of an SSE stream, which this synchronous adapter needs.
        # `create_new=True` starts a fresh server-side conversation on every
        # call: this project does not track `conversation_id` on its own side
        # (Ask is scoped to single-turn Q&A), so reusing Khoj's own
        # anonymous-mode default conversation would silently accumulate
        # unrelated history across unrelated questions instead.
        payload = {
            "q": f"{_NOTES_MODE_PREFIX} {question}",
            "n": limit,
            "stream": False,
            "create_new": True,
        }
        try:
            response = await self._http.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=CHAT_REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise KhojUnavailableError(f"Khoj chat failed: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise KhojUnavailableError(f"Khoj chat returned a non-JSON response: {exc}") from exc

        try:
            answer = data["response"]
            context = (data.get("references") or {}).get("context") or []
            references = tuple(
                KhojChatReference(
                    compiled=item["compiled"],
                    filename=item.get("file", ""),
                    heading=item.get("heading") or "",
                )
                for item in context
            )
        except (KeyError, TypeError) as exc:
            raise KhojUnavailableError(f"Khoj chat returned an unexpected shape: {exc}") from exc

        if not isinstance(answer, str):
            # khoj's own `MessageProcessor.handle_json_response` returns a
            # dict rather than text for an image-generation or error-detail
            # payload. Neither shape is a usable answer for this project's
            # text-only, notes-only Ask - and what an *unconfigured* chat
            # model actually returns was live-verified separately (a plain
            # HTTP 500, already handled by `raise_for_status()` above, not
            # this branch) - this remains a second, independent defense for
            # a *configured-but-degenerate* response, deliberately
            # conservative: any non-string "response" degrades exactly like
            # an unreachable Khoj, rather than being guessed at.
            raise KhojUnavailableError(
                f"Khoj chat returned a non-text response: {type(answer).__name__}"
            )

        return KhojChatResult(response=answer, references=references)
