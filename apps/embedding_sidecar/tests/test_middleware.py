"""Direct ASGI-level tests for `MaxBodySizeMiddleware`.

Driven at the raw `(scope, receive, send)` layer, not through FastAPI/
httpx - this is what lets the read-timeout test simulate a slow, chunked
body deterministically (a fake `receive` that sleeps between chunks)
without needing a real server or a flaky wall-clock race.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from starlette.types import Message, Receive, Scope, Send

from tc_embedding_sidecar.middleware import MaxBodySizeMiddleware


async def _echo_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Drains the body it is handed (mimicking what Starlette/FastAPI would
    do) and reports 200 - proves the middleware actually invoked the
    downstream app with a usable body, for the happy-path test."""
    total = 0
    more_body = True
    while more_body:
        message = await receive()
        total += len(message.get("body") or b"")
        more_body = message.get("more_body", False)
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(total).encode()})


def _http_scope() -> Scope:
    return {"type": "http", "method": "POST", "path": "/embed", "headers": []}


def _capturing_send() -> tuple[Send, list[Message]]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    return send, sent


def _status_of(sent: list[Message]) -> int:
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _chunks(*bodies: bytes) -> Callable[[], Awaitable[Message]]:
    """A `receive` that yields one `http.request` message per body in
    order, each with `more_body=True` except the last."""
    remaining = list(bodies)

    async def receive() -> Message:
        body = remaining.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(remaining)}

    return receive


async def test_a_body_within_both_budgets_reaches_the_downstream_app() -> None:
    send, sent = _capturing_send()
    middleware = MaxBodySizeMiddleware(_echo_app, max_bytes=100, max_read_seconds=5.0)

    await middleware(_http_scope(), _chunks(b"hello", b" world"), send)

    assert _status_of(sent) == 200
    body = next(m["body"] for m in sent if m["type"] == "http.response.body")
    assert body == str(len(b"hello world")).encode()


async def test_a_body_over_the_byte_limit_is_rejected_without_reaching_the_app() -> None:
    send, sent = _capturing_send()
    app_was_called = False

    async def app_that_must_not_run(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal app_was_called
        app_was_called = True

    middleware = MaxBodySizeMiddleware(app_that_must_not_run, max_bytes=5, max_read_seconds=5.0)

    await middleware(_http_scope(), _chunks(b"this is more than five bytes"), send)

    assert _status_of(sent) == 413
    assert not app_was_called


async def test_a_slow_chunked_body_times_out_before_the_byte_limit_is_reached() -> None:
    """A client trickling bytes slowly enough to never reach `max_bytes`,
    but slower than `max_read_seconds`, must still be cut off - the byte
    cap alone does not defend against a client that simply never finishes
    sending (a slowloris-style hold)."""
    chunks_read = 0

    async def slow_receive() -> Message:
        nonlocal chunks_read
        chunks_read += 1
        await asyncio.sleep(0.05)
        return {"type": "http.request", "body": b"x", "more_body": True}

    send, sent = _capturing_send()
    middleware = MaxBodySizeMiddleware(_echo_app, max_bytes=10_000, max_read_seconds=0.12)

    await middleware(_http_scope(), slow_receive, send)

    assert _status_of(sent) == 408
    # Proves it actually stopped early, not that it happened to finish
    # quickly for an unrelated reason.
    assert chunks_read < 10


async def test_non_http_scopes_pass_through_untouched() -> None:
    """e.g. a websocket scope - this middleware only concerns request
    bodies, which only exist for `http`."""
    called_with: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        called_with.append(scope)

    middleware = MaxBodySizeMiddleware(app, max_bytes=10, max_read_seconds=1.0)
    scope: Scope = {"type": "lifespan"}

    async def receive() -> Message:
        raise AssertionError("must not be called for a non-http scope")

    async def send(message: Message) -> None:
        raise AssertionError("must not be called for a non-http scope")

    await middleware(scope, receive, send)

    assert called_with == [scope]
