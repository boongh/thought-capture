"""A minimal OpenAI-compatible chat server, for configuring a real pinned
Khoj container's chat model in contract tests without a real provider
credential or cost (docs/adr/0003's "Ask proxy" amendment, "Chat contract
test").

Implements just enough of the OpenAI HTTP API for khoj's own
``khoj.utils.initialization._create_chat_configuration`` (first-boot,
``--non-interactive``) and its OpenAI conversation processor to complete a
real end-to-end chat call: ``GET /v1/models`` (model discovery at Khoj's
first boot) and ``POST /v1/chat/completions`` (both ``stream`` values, since
which one khoj's own outbound call uses is an internal detail this stub does
not need to assume). The answer text and "grounding" are entirely synthetic -
this stub never reads whatever context Khoj's prompt actually contains, it
always returns the same fixed response, deterministic enough for a test to
assert against.

Run standalone for manual/live verification:

    uv run uvicorn tests.contract.khoj.stub_chat_model:app --port 8899
"""

from __future__ import annotations

import json
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

MODEL_ID = "stub-chat-model"
ANSWER_TEXT = "This is a stub answer for contract testing."


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    return JSONResponse(
        {
            "object": "list",
            "data": [{"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "stub"}],
        }
    )


def _chat_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _non_streaming_response() -> JSONResponse:
    return JSONResponse(
        {
            "id": _chat_completion_id(),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": MODEL_ID,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": ANSWER_TEXT},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )


async def _streaming_chunks():  # type: ignore[no-untyped-def]
    completion_id = _chat_completion_id()
    created = int(time.time())

    def frame(delta: dict[str, object], finish_reason: str | None) -> str:
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": MODEL_ID,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(payload)}\n\n"

    # A role-only opening chunk, then the answer split into a few pieces, then
    # a finish chunk - matches the shape OpenAI's own streaming responses use
    # (a caller must handle a delta-only-once-per-field, multi-chunk answer,
    # not assume the whole answer arrives in one chunk).
    yield frame({"role": "assistant"}, None)
    for piece in (ANSWER_TEXT[: len(ANSWER_TEXT) // 2], ANSWER_TEXT[len(ANSWER_TEXT) // 2 :]):
        yield frame({"content": piece}, None)
    yield frame({}, "stop")
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):  # type: ignore[no-untyped-def]
    body = await request.json()
    if body.get("stream"):
        return StreamingResponse(_streaming_chunks(), media_type="text/event-stream")
    return _non_streaming_response()
