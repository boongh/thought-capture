"""Every /v1 endpoint, exercised against a real database.

The API is the seam a future UI is built on (docs/DESIGN.md 4.4), so its
contract - status codes, problem shapes, pagination, auth - is tested rather
than assumed.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_api.app import create_app
from tc_api.dependencies import ApiContext
from tc_application.capture import CaptureThought
from tc_domain.capture import UserId, WorkspaceId
from tc_domain.policy import AttachmentPolicy
from tc_infrastructure.config import Settings
from tc_infrastructure.db.outbox import PostgresOutbox
from tc_infrastructure.db.thought_reader import PostgresThoughtReader
from tc_infrastructure.db.thought_repository import PostgresThoughtRepository
from tests.unit.fakes import FakeAttachmentArchive

pytestmark = pytest.mark.integration

TOKEN = "test-bearer-token-value"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def api(
    app_session_factory: async_sessionmaker[AsyncSession],
    seeded_identity: tuple[uuid.UUID, uuid.UUID],
) -> AsyncIterator[httpx.AsyncClient]:
    """The real app, wired to the test database with a known bearer token.

    The lifespan is replaced so the test does not depend on a seeded Discord
    identity or a live HTTP client; everything else is the production code path.
    """
    workspace_id, user_id = seeded_identity
    settings = Settings(_env_file=None, api_bearer_token=TOKEN, workspace_timezone="Asia/Bangkok")

    context = ApiContext(
        settings=settings,
        capture=CaptureThought(
            PostgresThoughtRepository(app_session_factory),
            FakeAttachmentArchive(),
            AttachmentPolicy(max_bytes=1024),
        ),
        reader=PostgresThoughtReader(app_session_factory),
        outbox=PostgresOutbox(app_session_factory, lease_owner="test-api"),
        session_factory=app_session_factory,
        workspace_id=WorkspaceId(workspace_id),
        user_id=UserId(user_id),
    )

    @asynccontextmanager
    async def no_startup(_: object) -> AsyncIterator[None]:
        """Skip the production lifespan; the context is injected below."""
        yield

    app = create_app(lifespan_handler=no_startup)
    app.state.context = context

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def a_key() -> str:
    return f"api-{uuid.uuid4()}"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


async def test_liveness_needs_no_auth_and_touches_no_dependency(
    api: httpx.AsyncClient,
) -> None:
    response = await api.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_reports_the_database_and_outbox_depth(
    api: httpx.AsyncClient,
) -> None:
    response = await api.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert isinstance(body["pending_outbox_events"], int)


async def test_every_response_carries_a_request_id(api: httpx.AsyncClient) -> None:
    response = await api.get("/health/live")
    assert response.headers["X-Request-ID"]


async def test_a_supplied_request_id_is_echoed(api: httpx.AsyncClient) -> None:
    """Lets a caller correlate its own logs with the server's."""
    response = await api.get("/health/live", headers={"X-Request-ID": "abc123"})
    assert response.headers["X-Request-ID"] == "abc123"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="missing"),
        pytest.param({"Authorization": "Bearer wrong-token"}, id="wrong-token"),
        pytest.param({"Authorization": TOKEN}, id="no-scheme"),
        pytest.param({"Authorization": "Basic " + TOKEN}, id="wrong-scheme"),
        pytest.param({"Authorization": "Bearer "}, id="empty-token"),
    ],
)
async def test_capture_requires_a_valid_bearer_token(
    api: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    response = await api.post("/v1/thoughts", json={"body": "x"}, headers=headers)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_reads_require_authentication(api: httpx.AsyncClient) -> None:
    assert (await api.get("/v1/thoughts")).status_code == 401
    assert (await api.get("/v1/thoughts/1")).status_code == 401


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


async def test_capture_creates_a_thought(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/v1/thoughts",
        json={"body": "renew the passport"},
        headers={**AUTH, "Idempotency-Key": a_key()},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["thought_id"] > 0
    assert body["deduplicated"] is False


async def test_replaying_an_idempotency_key_returns_the_original(
    api: httpx.AsyncClient,
) -> None:
    """A retried request must not create a second thought (docs/DESIGN.md 1.1)."""
    key = a_key()
    payload = {"body": "exactly once"}

    first = await api.post("/v1/thoughts", json=payload, headers={**AUTH, "Idempotency-Key": key})
    second = await api.post("/v1/thoughts", json=payload, headers={**AUTH, "Idempotency-Key": key})

    assert first.json()["thought_id"] == second.json()["thought_id"]
    assert second.json()["deduplicated"] is True


async def test_capture_without_an_idempotency_key_is_refused(
    api: httpx.AsyncClient,
) -> None:
    response = await api.post("/v1/thoughts", json={"body": "x"}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["type"].endswith("idempotency-key-required")


async def test_empty_capture_is_refused(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/v1/thoughts", json={"body": "   "}, headers={**AUTH, "Idempotency-Key": a_key()}
    )
    assert response.status_code == 422
    assert response.json()["type"].endswith("capture-rejected")


async def test_unknown_timezone_is_refused(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/v1/thoughts",
        json={"body": "x", "client_timezone": "Mars/Olympus_Mons"},
        headers={**AUTH, "Idempotency-Key": a_key()},
    )
    assert response.status_code == 422
    assert response.json()["type"].endswith("unknown-timezone")


async def test_naive_timestamp_is_refused(api: httpx.AsyncClient) -> None:
    """Without an offset a thought cannot be filed under a local day."""
    response = await api.post(
        "/v1/thoughts",
        json={"body": "x", "client_created_at": "2026-08-30T13:00:00"},
        headers={**AUTH, "Idempotency-Key": a_key()},
    )
    assert response.status_code == 422
    assert response.json()["type"].endswith("naive-timestamp")


async def test_capture_derives_local_calendar_fields(api: httpx.AsyncClient) -> None:
    created = await api.post(
        "/v1/thoughts",
        json={"body": "evening thought", "client_created_at": "2026-08-30T13:00:00+00:00"},
        headers={**AUTH, "Idempotency-Key": a_key()},
    )
    thought_id = created.json()["thought_id"]

    fetched = await api.get(f"/v1/thoughts/{thought_id}", headers=AUTH)
    body = fetched.json()

    assert body["client_local_date"] == "2026-08-30"
    assert body["client_local_time"] == "20:00:00"
    assert body["client_timezone"] == "Asia/Bangkok"


async def test_a_correction_appends_rather_than_edits(api: httpx.AsyncClient) -> None:
    original = await api.post(
        "/v1/thoughts",
        json={"body": "teh meeting is at 4"},
        headers={**AUTH, "Idempotency-Key": a_key()},
    )
    original_id = original.json()["thought_id"]

    corrected = await api.post(
        "/v1/thoughts",
        json={"body": "the meeting is at 4", "correction_of": original_id},
        headers={**AUTH, "Idempotency-Key": a_key()},
    )
    corrected_id = corrected.json()["thought_id"]

    assert corrected_id != original_id
    still_there = await api.get(f"/v1/thoughts/{original_id}", headers=AUTH)
    assert still_there.json()["body"] == "teh meeting is at 4"
    assert (await api.get(f"/v1/thoughts/{corrected_id}", headers=AUTH)).json()[
        "correction_of"
    ] == original_id


async def test_the_raw_log_has_no_mutating_verbs(api: httpx.AsyncClient) -> None:
    """Append-only is a property of the API surface too (ADR-0001)."""
    created = await api.post(
        "/v1/thoughts", json={"body": "immutable"}, headers={**AUTH, "Idempotency-Key": a_key()}
    )
    thought_id = created.json()["thought_id"]

    for method in ("put", "patch", "delete"):
        response = await getattr(api, method)(f"/v1/thoughts/{thought_id}", headers=AUTH)
        assert response.status_code == 405, f"{method.upper()} must not be routed"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_get_unknown_thought_is_not_found(api: httpx.AsyncClient) -> None:
    response = await api.get("/v1/thoughts/999999999", headers=AUTH)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_list_returns_newest_first(api: httpx.AsyncClient) -> None:
    ids = []
    for index in range(3):
        created = await api.post(
            "/v1/thoughts",
            json={"body": f"ordered {index}"},
            headers={**AUTH, "Idempotency-Key": a_key()},
        )
        ids.append(created.json()["thought_id"])

    listed = await api.get("/v1/thoughts?limit=3", headers=AUTH)
    returned = [item["id"] for item in listed.json()["items"]]

    assert returned == sorted(returned, reverse=True)
    assert returned[0] == max(ids)


async def test_pagination_walks_the_whole_log_without_repeats(
    api: httpx.AsyncClient,
) -> None:
    for index in range(5):
        await api.post(
            "/v1/thoughts",
            json={"body": f"page {index}"},
            headers={**AUTH, "Idempotency-Key": a_key()},
        )

    seen: list[int] = []
    cursor: str | None = None
    for _ in range(10):  # bounded, so a cursor bug cannot loop forever
        url = "/v1/thoughts?limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = (await api.get(url, headers=AUTH)).json()
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert cursor is None, "pagination did not terminate"
    assert len(seen) == len(set(seen)), "a thought appeared on two pages"


async def test_a_malformed_cursor_is_a_bad_request(api: httpx.AsyncClient) -> None:
    response = await api.get("/v1/thoughts?cursor=not-a-cursor", headers=AUTH)
    assert response.status_code == 400
    assert response.json()["type"].endswith("invalid-cursor")


async def test_local_date_filters_apply(api: httpx.AsyncClient) -> None:
    await api.post(
        "/v1/thoughts",
        json={"body": "on the day", "client_created_at": "2026-03-15T05:00:00+00:00"},
        headers={**AUTH, "Idempotency-Key": a_key()},
    )

    included = await api.get("/v1/thoughts?from=2026-03-15&to=2026-03-15", headers=AUTH)
    excluded = await api.get("/v1/thoughts?from=2026-03-16&to=2026-03-16", headers=AUTH)

    assert any(item["body"] == "on the day" for item in included.json()["items"])
    assert all(item["body"] != "on the day" for item in excluded.json()["items"])


async def test_an_inverted_date_range_is_a_bad_request(api: httpx.AsyncClient) -> None:
    response = await api.get("/v1/thoughts?from=2026-03-16&to=2026-03-15", headers=AUTH)
    assert response.status_code == 400
    assert response.json()["type"].endswith("invalid-range")


async def test_source_filter_applies(api: httpx.AsyncClient) -> None:
    await api.post(
        "/v1/thoughts", json={"body": "from api"}, headers={**AUTH, "Idempotency-Key": a_key()}
    )

    api_sourced = await api.get("/v1/thoughts?source=api", headers=AUTH)
    discord_sourced = await api.get("/v1/thoughts?source=discord", headers=AUTH)

    assert all(item["source"] == "api" for item in api_sourced.json()["items"])
    assert all(item["source"] == "discord" for item in discord_sourced.json()["items"])


async def test_limit_is_bounded(api: httpx.AsyncClient) -> None:
    """An unbounded limit is a denial-of-service against our own database."""
    response = await api.get("/v1/thoughts?limit=100000", headers=AUTH)
    assert response.status_code == 422
    assert response.json()["type"].endswith("validation-failed")


async def test_timestamps_are_returned_with_offsets(api: httpx.AsyncClient) -> None:
    created = await api.post(
        "/v1/thoughts", json={"body": "offsets"}, headers={**AUTH, "Idempotency-Key": a_key()}
    )
    fetched = await api.get(f"/v1/thoughts/{created.json()['thought_id']}", headers=AUTH)

    parsed = dt.datetime.fromisoformat(fetched.json()["received_at"])
    assert parsed.tzinfo is not None, "timestamps must carry a UTC offset"
