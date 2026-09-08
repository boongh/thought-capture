"""A hard byte ceiling on the request body, enforced before it is parsed.

FastAPI/Pydantic buffer and fully parse the JSON body before any
endpoint-level validation (`app.py`'s `max_batch_size`/`max_text_length`
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
either never invoked at all (oversized - this middleware answers 413
itself) or invoked with a normal, already-fully-read body (replayed as a
single `http.request` message), so FastAPI never observes a failure from
this middleware at all.
"""

from __future__ import annotations

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class MaxBodySizeMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        total = 0
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] != "http.request":
                # e.g. the client disconnected mid-body - nothing to reject
                # and no app to hand a body to; let the connection end.
                return
            chunk = message.get("body") or b""
            total += len(chunk)
            if total > self.max_bytes:
                response = PlainTextResponse(
                    f"request body exceeds the {self.max_bytes}-byte limit", status_code=413
                )
                await response(scope, receive, send)
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
