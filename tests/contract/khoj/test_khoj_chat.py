"""``HttpKhojClient.chat`` against a real, pinned Khoj instance configured with
a real (if stubbed) chat model (docs/adr/0003's "Ask proxy" amendment, "Chat
contract test").

Requires Khoj to be configured against `tests/contract/khoj/stub_chat_model.py`
at its own first `--non-interactive` boot - the streaming wire format below
(`metadata`/`status`/`references`/raw-text `message` chunks/
`start_llm_response`/`end_llm_response`/`usage`/`end_response`) was read
directly from a real run against that exact setup, not assumed from source.
Locally:

    uv run uvicorn tests.contract.khoj.stub_chat_model:app --port 8899 &
    TC_KHOJ_OPENAI_BASE_URL=http://host.docker.internal:8899/v1 \\
    TC_KHOJ_OPENAI_API_KEY=stub-key \\
    docker compose --env-file .env -f deploy/compose/docker-compose.yml \\
      -f deploy/compose/khoj.docker-compose.yml --profile ai up -d

A Khoj instance whose database volume was ever booted *without* those two
variables set has already registered a different (or no) chat model and will
not pick up the stub - wipe `khoj-db-data`/`khoj-home` (`down -v`) first.
"""

from __future__ import annotations

import httpx
import pytest

from tc_domain.khoj_ports import KhojIndexFile
from tc_infrastructure.khoj.client import HttpKhojClient
from tests.contract.khoj.conftest import KHOJ_BASE_URL
from tests.contract.khoj.stub_chat_model import ANSWER_TEXT

pytestmark = pytest.mark.contract


def _file(unique: str) -> KhojIndexFile:
    return KhojIndexFile(
        filename=f"contract-test/{unique}.md",
        content=f"# Chat contract test {unique}\n\nThe keyword is {unique}.\n".encode(),
    )


async def test_chat_answers_and_references_the_indexed_note(
    khoj_client: HttpKhojClient, unique: str
) -> None:
    file = _file(unique)
    await khoj_client.index((file,))

    chunks = [chunk async for chunk in khoj_client.chat(f"what is the keyword for {unique}")]

    text = "".join(c.text_delta for c in chunks)
    assert ANSWER_TEXT in text
    assert chunks[-1].done is True

    reference_chunks = [c for c in chunks if c.references]
    assert reference_chunks, "expected a references event for content that was just indexed"
    references = reference_chunks[0].references
    assert references is not None
    assert any(r.filename == file.filename for r in references)
    assert any(unique in r.compiled for r in references)


async def test_chat_leaves_no_conversation_behind(
    khoj_client: HttpKhojClient, unique: str, http: httpx.AsyncClient
) -> None:
    """Ask's retention policy (docs/adr/0003's "Ask proxy" amendment,
    "Conversation retention"): every chat call deletes its own server-side
    conversation once the answer is fully consumed - no additional,
    unaccounted-for copy of personal-memory content survives an Ask call."""
    before = await http.get(f"{KHOJ_BASE_URL}/api/chat/sessions")
    before.raise_for_status()
    ids_before = {session["conversation_id"] for session in before.json()}

    async for _ in khoj_client.chat(f"question about {unique}"):
        pass

    after = await http.get(f"{KHOJ_BASE_URL}/api/chat/sessions")
    after.raise_for_status()
    ids_after = {session["conversation_id"] for session in after.json()}

    assert ids_after - ids_before == set(), "chat() must not leave a new conversation behind"
