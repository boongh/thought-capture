"""Khoj HTTP client, implementing ``KhojPort`` (docs/adr/0003).

Endpoints and shapes confirmed against a real running
``ghcr.io/khoj-ai/khoj:2.0.0-beta.28`` container (docs/adr/0003's contract
spike), not inferred from documentation alone. No auth header is sent: Khoj
runs ``--anonymous-mode``, loopback-only (docs/adr/0003's auth-mode
decision) - the base URL is the only configuration this adapter needs.

``chat``'s streaming wire format was read directly from khoj-ai/khoj's source
at the pinned tag, not observed live (docs/adr/0003's "Ask proxy" amendment
explains why that is a different, weaker evidence standard than the rest of
this adapter, and names the follow-up contract test that closes the gap):
``ChatEvent`` (``src/khoj/processor/conversation/utils.py``) delimits each
event with ``END_EVENT = "␃🔚␗"``; a ``MESSAGE`` event's payload is the raw
answer-text delta itself, every other event type is JSON
``{"type": ..., "data": ...}``. This adapter only needs three of those event
types: ``metadata`` (carries Khoj's own ``conversationId``, needed for the
delete-after-use retention fix below), ``references`` (the grounding notes),
and the un-typed raw-text ``message`` deltas.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx

from tc_domain.khoj_ports import (
    KhojChatChunk,
    KhojChatReference,
    KhojIndexFile,
    KhojSearchResult,
    KhojUnavailableError,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 30.0
# /api/chat can involve a live LLM call once Khoj has a chat model configured
# (docs/adr/0003 finding 4 and its "Ask proxy" amendment); generous relative
# to the search/content endpoints, which never call out to a model at all.
# Applies per network read while streaming, not to the whole conversation -
# httpx's default timeout shape (a slow-starting model does not itself time
# out a call that is otherwise still receiving chunks).
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

# khoj.processor.conversation.utils.ChatEvent.END_EVENT, verified at the
# pinned tag - a deliberately unusual delimiter unlikely to appear in real
# answer text.
_END_EVENT = "␃\U0001f51a␗"


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

    async def chat(self, question: str, *, limit: int = 5) -> AsyncIterator[KhojChatChunk]:
        # `create_new=True` starts a fresh server-side conversation on every
        # call: this project does not track `conversation_id` across calls
        # (Ask is scoped to single-turn Q&A), so reusing Khoj's own
        # anonymous-mode default conversation would silently accumulate
        # unrelated history across unrelated questions. `stream=True` is what
        # the accepted Ask/API contract requires (docs/DESIGN.md 7.6) and is
        # also the only response shape that surfaces Khoj's own
        # `conversationId` (the "metadata" event below) - needed to delete
        # the conversation immediately after use, see `_delete_conversation`.
        payload = {
            "q": f"{_NOTES_MODE_PREFIX} {question}",
            "n": limit,
            "stream": True,
            "create_new": True,
        }
        parser = _ChatEventParser()
        buffer = ""
        try:
            async with self._http.stream(
                "POST",
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=CHAT_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                response.raise_for_status()
                async for text in response.aiter_text():
                    buffer += text
                    while _END_EVENT in buffer:
                        raw_event, buffer = buffer.split(_END_EVENT, 1)
                        if not raw_event:
                            continue
                        chunk = parser.parse_event(raw_event)
                        if chunk is not None:
                            yield chunk
                if buffer.strip():
                    # A stream that ends without a final END_EVENT delimiter
                    # still has one more event's worth of content to parse -
                    # matches `read_chat_stream`'s own "process any remaining
                    # data in the buffer" step.
                    chunk = parser.parse_event(buffer)
                    if chunk is not None:
                        yield chunk
        except httpx.HTTPError as exc:
            raise KhojUnavailableError(f"Khoj chat failed: {exc}") from exc
        finally:
            if parser.conversation_id is not None:
                await self._delete_conversation(parser.conversation_id)

        yield KhojChatChunk(done=True)

    async def _delete_conversation(self, conversation_id: str) -> None:
        """Ask's own retention policy (docs/adr/0003's "Ask proxy" amendment,
        "Conversation retention"): leave no server-side conversation behind
        Khoj's anonymous default user after a single Ask call - an additional
        copy of personal-memory content this project's own backup/export/
        retention story (docs/DESIGN.md 12.3, 14.3) does not otherwise cover.
        Best-effort: a cleanup failure must not mask the answer (or lack of
        one) the caller already has, so this logs rather than raises.
        """
        try:
            response = await self._http.delete(
                f"{self._base_url}/api/chat/history",
                params={"conversation_id": conversation_id},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "khoj_chat.conversation_cleanup_failed",
                extra={"error_class": type(exc).__name__},
            )


def _try_parse_json_object(raw: str) -> dict[str, object] | None:
    """Mirrors khoj's own `MessageProcessor.convert_message_chunk_to_json`
    heuristic: brace-delimited and JSON-parseable as an object, or this is
    raw message text, not a typed event."""
    stripped = raw.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


# The complete set of event types khoj's `ChatEvent`/`send_event` ever emits
# (khoj.processor.conversation.utils, khoj.routers.api_chat, pinned tag) -
# not just the ones this adapter surfaces. Used to tell a genuine (if
# unhandled) khoj event apart from raw MESSAGE-event answer text that merely
# happens to parse as a JSON object with a "type"-named key - a coincidence
# `_ChatEventParser.parse_event` must not mistake for a real event, whether
# or not that coincidental value happens to collide with a name on this list.
_KNOWN_EVENT_TYPES = frozenset(
    {
        "metadata",
        "references",
        "message",
        "start_llm_response",
        "end_llm_response",
        "usage",
        "end_response",
        "status",
        "thought",
        "generated_assets",
        "interrupt",
    }
)


class _ChatEventParser:
    """Stateful parser for khoj's `END_EVENT`-delimited chat stream.

    Needs state, not just per-chunk inspection, because of a second layer of
    the same coincidental-JSON problem `_KNOWN_EVENT_TYPES` already guards
    against: the live-verified event order (docs/adr/0003's "Ask proxy"
    amendment, contract-test capture) shows raw MESSAGE-event answer text
    only ever streams strictly between `start_llm_response` and
    `end_llm_response`, and no other event type is ever emitted in that
    window. So an answer chunk that happens to parse as
    `{"type": "status", "data": "42"}` mid-answer is indistinguishable from a
    real `status` event by shape alone - both are "a known type name, not
    metadata/references/message". Tracking whether we're inside that window
    resolves the ambiguity the same way khoj's own protocol does: once
    `start_llm_response` has been seen and `end_llm_response` has not, only
    `end_llm_response` itself may still be treated as a control event: every
    other chunk - whatever it happens to parse as - is answer text.
    """

    def __init__(self) -> None:
        self.conversation_id: str | None = None
        self._in_llm_response = False

    def parse_event(self, raw_event: str) -> KhojChatChunk | None:
        parsed = _try_parse_json_object(raw_event)
        if parsed is None:
            # Not JSON-shaped at all: khoj's own `send_event` yields a
            # MESSAGE event's payload unwrapped (unlike every other event
            # type), so an un-typed chunk is always a raw answer-text delta.
            return KhojChatChunk(text_delta=raw_event)

        event_type = parsed.get("type")
        data = parsed.get("data")

        if not isinstance(event_type, str) or event_type not in _KNOWN_EVENT_TYPES:
            # Either there's no "type" key at all, its value isn't a string
            # (a real khoj event's "type" always is - checked first so a
            # non-string value, e.g. a raw answer that happens to be
            # `{"type": [...], ...}`, short-circuits before the `in` check
            # below, which would otherwise raise TypeError on an unhashable
            # list/dict), or the string value is not one khoj actually
            # emits. Either way this cannot be a genuine typed event, so it
            # must be raw MESSAGE-event answer text that happens to be
            # JSON-shaped.
            return KhojChatChunk(text_delta=raw_event)

        if self._in_llm_response and event_type != "end_llm_response":
            # Inside the answer-streaming window, the only confirmed control
            # event is `end_llm_response` itself - anything else with a
            # recognized "type" is a coincidental collision with real answer
            # text (the P1 bug: a raw MESSAGE chunk like
            # `{"type": "status", "data": "42"}` must not be dropped just
            # because "status" is a name khoj happens to use elsewhere).
            return KhojChatChunk(text_delta=raw_event)

        if event_type == "metadata":
            if isinstance(data, dict):
                candidate = data.get("conversationId")
                if isinstance(candidate, str) and candidate:
                    self.conversation_id = candidate
            return None

        if event_type == "references":
            return KhojChatChunk(references=_parse_chat_references(data))

        if event_type == "message":
            # A JSON-shaped message chunk happens too (the answer text
            # itself starts with "{" and ends with "}") - use `data` as the
            # text delta the same way khoj's own MessageProcessor does,
            # rather than silently dropping it.
            text = data if isinstance(data, str) else json.dumps(data)
            return KhojChatChunk(text_delta=text)

        if event_type == "start_llm_response":
            self._in_llm_response = True
            return None

        if event_type == "end_llm_response":
            self._in_llm_response = False
            return None

        return None


def _parse_chat_references(data: object) -> tuple[KhojChatReference, ...]:
    if not isinstance(data, dict):
        raise KhojUnavailableError(
            f"Khoj chat 'references' event had an unexpected shape: {type(data).__name__}"
        )
    context = data.get("context")
    if context is None:
        return ()
    if not isinstance(context, list):
        # The exact malformed shape independent review caught: a prior
        # version assumed `references`/`context` were always dict/list and
        # let a mismatch surface as a bare, uncaught `AttributeError` instead
        # of degrading like every other malformed-response case.
        raise KhojUnavailableError(
            f"Khoj chat 'references' context was not a list: {type(context).__name__}"
        )
    try:
        return tuple(
            KhojChatReference(
                compiled=item["compiled"],
                filename=item.get("file", ""),
                heading=item.get("heading") or "",
            )
            for item in context
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise KhojUnavailableError(
            f"Khoj chat returned an unexpected reference shape: {exc}"
        ) from exc
