"""A hard byte ceiling and read-time budget on the request body, both
enforced before it is parsed.

FastAPI/Pydantic buffer and fully parse the JSON body before any
endpoint-level validation (`app.py`'s `max_batch_size`/`max_text_bytes`
checks) ever runs - by the time those checks see the request, an
oversized body has already been read into memory in full. A
`Content-Length` header check alone is not enough either: a chunked-
transfer-encoded request declares no length at all.

This buffers the body itself, in this middleware, before the downstream
FastAPI app is ever invoked - it does *not* use the more obvious-looking
approach of wrapping `receive()` to raise once a running total is
exceeded and letting that exception propagate up through the app. That
approach does not work here: FastAPI's own request handler reads the body
via `Request.json()` wrapped in a broad `except Exception` that converts
*any* failure during body reading into its own generic 400 "there was an
error parsing the body" response, before an exception raised from inside
`receive()` ever gets a chance to reach this middleware's own error
handling. Buffering first sidesteps that entirely: the downstream app is
either never invoked at all (oversized, or timed out - this middleware
answers 413/408 itself) or invoked with a normal, already-fully-read body
(replayed as a single `http.request` message), so FastAPI never observes a
failure from this middleware at all.

The byte cap alone only defends against a body that is too *large* - a
client that sends valid, under-the-cap bytes at a trickle (one byte every
few seconds, indefinitely) would otherwise hold its buffering slot open
forever. `max_read_seconds` bounds the *total time* this middleware will
spend waiting for a body to finish arriving, closing that gap.

Neither of these caps how many requests may be doing this concurrently -
that is `settings.limit_concurrency`, enforced by uvicorn itself
(`__main__.py`) before a connection past that ceiling ever reaches this
middleware, or any other code in this process, at all.
"""

from __future__ import annotations

import asyncio

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class MaxBodySizeMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int, max_read_seconds: float) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.max_read_seconds = max_read_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        total = 0
        more_body = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.max_read_seconds
        while more_body:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._respond(scope, send, "request body read timed out", 408)
                return
            try:
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except TimeoutError:
                await self._respond(scope, send, "request body read timed out", 408)
                return
            if message["type"] != "http.request":
                # e.g. the client disconnected mid-body - nothing to reject
                # and no app to hand a body to; let the connection end.
                return
            chunk = message.get("body") or b""
            total += len(chunk)
            if total > self.max_bytes:
                await self._respond(
                    scope, send, f"request body exceeds the {self.max_bytes}-byte limit", 413
                )
                return
            chunks.append(chunk)
            more_body = message.get("more_body", False)

        buffered = b"".join(chunks)
        already_replayed = False

        async def replay_receive() -> Message:
            nonlocal already_replayed
            if not already_replayed:
                already_replayed = True
                return {"type": "http.request", "body": buffered, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _respond(scope: Scope, send: Send, detail: str, status_code: int) -> None:
        response = PlainTextResponse(detail, status_code=status_code)
        # `receive` is unused by a plain response send, but ASGI apps are
        # callable as `(scope, receive, send)`; PlainTextResponse only
        # needs `send`, so pass a receive that will never actually be
        # awaited to satisfy that shape without reusing the real one.
        await response(scope, _unused_receive, send)


async def _unused_receive() -> Message:  # pragma: no cover - never actually awaited
    raise AssertionError("PlainTextResponse does not read the request body")
