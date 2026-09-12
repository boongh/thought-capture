"""``StartReembedRun``/``ReconcileReembedRuns`` orchestration, driven with
fakes (F15-A, docs/plans/embedding-sync-review-round-4.md) - same test
shapes as ``tests/unit/test_embedding_sync.py``.

Fakes are defined locally rather than added to ``tests/unit/fakes.py``: this
file owns only itself and
``packages/application/src/tc_application/reembed.py`` per the fix brief,
and ``tests/unit/fakes.py`` is shared with other in-flight work.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tc_application.reembed import DEAD_LETTERED_ERROR_CODE, ReconcileReembedRuns, StartReembedRun
from tc_domain.embedding_ports import EmbeddingUnavailableError, ReembedRun

WORKSPACE_ID = uuid.uuid4()
RUN_ID = uuid.uuid4()
MODEL_ID = "thenlper/gte-small@abc123"
STARTED_AT = datetime(2026, 9, 12, tzinfo=UTC)


class FakeEmbeddingPort:
    """Returns a fixed current model id, unless told to fail."""

    def __init__(self, *, model_id: str = MODEL_ID, raises: Exception | None = None) -> None:
        self.model_id = model_id
        self.raises = raises
        self.current_model_id_calls = 0

    async def embed(self, texts: tuple[str, ...]) -> tuple[object, ...]:
        raise NotImplementedError("not exercised by these tests")

    async def current_model_id(self) -> str:
        self.current_model_id_calls += 1
        if self.raises is not None:
            raise self.raises
        return self.model_id


class FakeReembedRunStore:
    """Records calls; state is fully caller-controlled rather than derived,
    since these tests exist to prove the *application layer's* branching,
    not a real store's SQL."""

    def __init__(
        self,
        *,
        running: ReembedRun | None = None,
        started: ReembedRun | None = None,
        list_running_result: tuple[ReembedRun, ...] = (),
        current_documents: dict[uuid.UUID, int] | None = None,
        embedded_under_model: dict[tuple[uuid.UUID, str], int] | None = None,
        dead_lettered_at: dict[uuid.UUID, datetime] | None = None,
    ) -> None:
        self._running = running
        self._started = started
        self._list_running_result = list_running_result
        self._current_documents = current_documents or {}
        self._embedded_under_model = embedded_under_model or {}
        # Maps workspace_id -> when its dead-lettered event was created, so
        # tests can prove `since` actually excludes a dead letter that
        # predates the run being reconciled (a stale, unrelated failure).
        self._dead_lettered_at = dead_lettered_at or {}
        self.start_calls: list[tuple[uuid.UUID, str]] = []
        self.succeeded: list[uuid.UUID] = []
        self.failed: list[dict[str, object]] = []

    async def start(self, workspace_id: uuid.UUID, *, embedding_model_id: str) -> ReembedRun:
        self.start_calls.append((workspace_id, embedding_model_id))
        if self._running is not None:
            return self._running
        assert self._started is not None
        return self._started

    async def find_running(self, workspace_id: uuid.UUID) -> ReembedRun | None:
        return self._running

    async def list_running(self) -> tuple[ReembedRun, ...]:
        return self._list_running_result

    async def count_current_documents(self, workspace_id: uuid.UUID) -> int:
        return self._current_documents.get(workspace_id, 0)

    async def count_embedded_under_model(
        self, workspace_id: uuid.UUID, embedding_model_id: str
    ) -> int:
        return self._embedded_under_model.get((workspace_id, embedding_model_id), 0)

    async def has_dead_lettered_sync_events(
        self, workspace_id: uuid.UUID, *, since: datetime
    ) -> bool:
        dead_lettered_at = self._dead_lettered_at.get(workspace_id)
        return dead_lettered_at is not None and dead_lettered_at >= since

    async def mark_succeeded(self, run_id: uuid.UUID) -> None:
        self.succeeded.append(run_id)

    async def mark_failed(self, run_id: uuid.UUID, *, error_code: str) -> None:
        self.failed.append({"run_id": run_id, "error_code": error_code})


def _run(
    *, status: str = "running", enqueued: int = 2, started_at: datetime = STARTED_AT
) -> ReembedRun:
    return ReembedRun(
        run_id=RUN_ID,
        workspace_id=WORKSPACE_ID,
        embedding_model_id=MODEL_ID,
        status=status,
        enqueued=enqueued,
        started_at=started_at,
    )


async def test_start_asks_the_sidecar_for_the_target_model_and_starts_the_run() -> None:
    embed = FakeEmbeddingPort()
    runs = FakeReembedRunStore(started=_run())
    start = StartReembedRun(embed=embed, runs=runs)

    result = await start(WORKSPACE_ID)

    assert result.embedding_model_id == MODEL_ID
    assert runs.start_calls == [(WORKSPACE_ID, MODEL_ID)]


async def test_start_never_falls_back_to_a_guessed_model_when_the_sidecar_is_unreachable() -> None:
    embed = FakeEmbeddingPort(raises=EmbeddingUnavailableError("sidecar down"))
    runs = FakeReembedRunStore(started=_run())
    start = StartReembedRun(embed=embed, runs=runs)

    with pytest.raises(EmbeddingUnavailableError):
        await start(WORKSPACE_ID)

    # The store must never even be asked to start a run against a guessed
    # model id - the sidecar failure must short-circuit before that.
    assert runs.start_calls == []


async def test_start_is_idempotent_while_a_run_is_already_running() -> None:
    already_running = _run(status="running", enqueued=5)
    embed = FakeEmbeddingPort()
    runs = FakeReembedRunStore(running=already_running)
    start = StartReembedRun(embed=embed, runs=runs)

    result = await start(WORKSPACE_ID)

    assert result is already_running
    assert result.enqueued == 5


async def test_start_returns_an_already_running_run_without_asking_the_sidecar() -> None:
    """F21 (docs/plans/embedding-sync-review-round-5.md): an operator retrying
    the endpoint during exactly the outage that motivates the retry must not
    get a 503 for a sweep that is already running - the store's own record of
    a running run must be consulted before the sidecar, not after."""
    already_running = _run(status="running", enqueued=5)
    embed = FakeEmbeddingPort(raises=EmbeddingUnavailableError("sidecar down"))
    runs = FakeReembedRunStore(running=already_running)
    start = StartReembedRun(embed=embed, runs=runs)

    result = await start(WORKSPACE_ID)

    assert result is already_running
    assert embed.current_model_id_calls == 0
    assert runs.start_calls == []


async def test_start_still_propagates_a_real_outage_when_no_run_is_running() -> None:
    """The fix must not swallow a genuine outage: absent a running run, an
    unreachable sidecar still surfaces as EmbeddingUnavailableError."""
    embed = FakeEmbeddingPort(raises=EmbeddingUnavailableError("sidecar down"))
    runs = FakeReembedRunStore(started=_run())
    start = StartReembedRun(embed=embed, runs=runs)

    with pytest.raises(EmbeddingUnavailableError):
        await start(WORKSPACE_ID)

    assert runs.start_calls == []


async def test_reconcile_marks_succeeded_once_every_current_document_is_embedded() -> None:
    run = _run()
    runs = FakeReembedRunStore(
        list_running_result=(run,),
        current_documents={WORKSPACE_ID: 3},
        embedded_under_model={(WORKSPACE_ID, MODEL_ID): 3},
    )
    reconcile = ReconcileReembedRuns(runs)

    await reconcile()

    assert runs.succeeded == [RUN_ID]
    assert runs.failed == []


async def test_reconcile_leaves_a_run_running_when_documents_are_not_all_embedded_yet() -> None:
    run = _run()
    runs = FakeReembedRunStore(
        list_running_result=(run,),
        current_documents={WORKSPACE_ID: 3},
        embedded_under_model={(WORKSPACE_ID, MODEL_ID): 1},
        # No dead letter yet either - genuinely still in progress.
    )
    reconcile = ReconcileReembedRuns(runs)

    await reconcile()

    assert runs.succeeded == []
    assert runs.failed == []


async def test_reconcile_marks_failed_when_this_runs_own_sync_event_has_dead_lettered() -> None:
    run = _run()
    runs = FakeReembedRunStore(
        list_running_result=(run,),
        current_documents={WORKSPACE_ID: 3},
        embedded_under_model={(WORKSPACE_ID, MODEL_ID): 1},
        dead_lettered_at={WORKSPACE_ID: STARTED_AT + timedelta(minutes=1)},
    )
    reconcile = ReconcileReembedRuns(runs)

    await reconcile()

    assert runs.succeeded == []
    assert runs.failed == [{"run_id": RUN_ID, "error_code": DEAD_LETTERED_ERROR_CODE}]


async def test_reconcile_ignores_a_dead_letter_from_before_this_run_started() -> None:
    """A stray dead-lettered event from an earlier, unrelated failure (e.g.
    the exact F13-A model-conflict halt this feature exists to recover from)
    must never poison a brand-new reembed sweep - only a dead letter created
    at or after this run's own `started_at` counts (round-4 review finding,
    confirmed by two independent re-reviews)."""
    run = _run()
    runs = FakeReembedRunStore(
        list_running_result=(run,),
        current_documents={WORKSPACE_ID: 3},
        # Nothing embedded yet - the sweep has barely started, exactly the
        # state a reconcile pass would see on its very first poll.
        embedded_under_model={(WORKSPACE_ID, MODEL_ID): 0},
        dead_lettered_at={WORKSPACE_ID: STARTED_AT - timedelta(days=7)},
    )
    reconcile = ReconcileReembedRuns(runs)

    await reconcile()

    assert runs.succeeded == []
    assert runs.failed == []


async def test_reconcile_prefers_succeeded_over_a_dead_letter_found_in_the_same_pass() -> None:
    """A dead-lettered retry from an earlier, since-superseded attempt must
    not fail a run that has already achieved its goal - success is judged
    by the embedded-count invariant, not the absence of any past failure."""
    run = _run()
    runs = FakeReembedRunStore(
        list_running_result=(run,),
        current_documents={WORKSPACE_ID: 3},
        embedded_under_model={(WORKSPACE_ID, MODEL_ID): 3},
        dead_lettered_at={WORKSPACE_ID: STARTED_AT + timedelta(minutes=1)},
    )
    reconcile = ReconcileReembedRuns(runs)

    await reconcile()

    assert runs.succeeded == [RUN_ID]
    assert runs.failed == []


async def test_reconcile_with_no_running_runs_does_nothing() -> None:
    runs = FakeReembedRunStore(list_running_result=())
    reconcile = ReconcileReembedRuns(runs)

    await reconcile()

    assert runs.succeeded == []
    assert runs.failed == []


async def test_no_document_content_ever_reaches_a_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither ``StartReembedRun`` nor ``ReconcileReembedRuns`` emits any log
    record at all in the ordinary path - there is nothing here that logs
    document bodies or embedding values to begin with, so the invariant
    (docs/DESIGN.md 14.2) holds trivially. Asserted explicitly, the same way
    ``tests/unit/test_embedding_sync.py`` asserts it for the sync path,
    rather than assumed."""
    run = _run()
    runs = FakeReembedRunStore(
        started=run,
        list_running_result=(run,),
        current_documents={WORKSPACE_ID: 1},
        embedded_under_model={(WORKSPACE_ID, MODEL_ID): 1},
    )
    start = StartReembedRun(embed=FakeEmbeddingPort(), runs=runs)
    reconcile = ReconcileReembedRuns(runs)

    with caplog.at_level("DEBUG"):
        await start(WORKSPACE_ID)
        await reconcile()

    assert caplog.records == []
