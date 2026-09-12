"""``POST /v1/admin/reembed`` and its worker-side reconciliation (F15-A,
docs/plans/embedding-sync-review-round-4.md; docs/DESIGN.md 8.5, 9.2).

Deliberately a separate admin action from ``ForceEmbeddingSync``
(``POST /v1/admin/embedding-sync``, docs/DESIGN.md 10) - that endpoint stays
an enqueue-only trigger sharing one code path with the organize writer's
per-revision enqueue. This one additionally wipes the workspace's existing
``document_embeddings`` and records a tracked ``runs(kind='reembed')`` row,
because it exists specifically to migrate a workspace onto a new embedding
model (docs/DESIGN.md 8.5, 9.2's single-model-per-workspace invariant).
"""

from __future__ import annotations

from tc_domain.capture import WorkspaceId
from tc_domain.embedding_ports import EmbeddingPort, ReembedRun, ReembedRunStore

# Recorded on a run this sweep marks failed - never personal content, just a
# fixed operator-facing reason code (docs/DESIGN.md 14.2).
DEAD_LETTERED_ERROR_CODE = "embedding_sync_dead_lettered"


class StartReembedRun:
    """``POST /v1/admin/reembed``: begin (or return the already-running)
    tracked reembed sweep for a workspace, targeting whatever model the
    embedding sidecar currently serves."""

    def __init__(self, *, embed: EmbeddingPort, runs: ReembedRunStore) -> None:
        self._embed = embed
        self._runs = runs

    async def __call__(self, workspace_id: WorkspaceId) -> ReembedRun:
        # Consult the store before the sidecar: an operator retrying this
        # endpoint during exactly the outage that motivates the retry must
        # get back the already-running sweep, not a 503, since idempotency
        # matters most when the sidecar is down (F21,
        # docs/plans/embedding-sync-review-round-5.md). `start`'s own
        # running check still covers the concurrent two-callers-at-once race
        # that this lookup can't.
        existing = await self._runs.find_running(workspace_id)
        if existing is not None:
            return existing
        # No run running: an unreachable sidecar here is a real outage and
        # must fail loudly (the caller maps EmbeddingUnavailableError to a
        # 503) rather than ever recording a run against a guessed model.
        embedding_model_id = await self._embed.current_model_id()
        return await self._runs.start(workspace_id, embedding_model_id=embedding_model_id)


class ReconcileReembedRuns:
    """Called by the worker after each embedding-sync poll cycle: advances
    every workspace's ``running`` reembed run to ``succeeded``/``failed``
    once its outcome is known. A no-op call (no runs currently running) is
    the common case, not an error.

    The succeeded/failed/still-running decision lives here, not in the
    store, the same way ``DeliverEmbeddingSync`` above decides outcomes by
    composing narrow port calls rather than asking a store to decide for
    it - so this branching is testable against fakes without a real
    database.
    """

    def __init__(self, runs: ReembedRunStore) -> None:
        self._runs = runs

    async def __call__(self) -> None:
        for run in await self._runs.list_running():
            current = await self._runs.count_current_documents(run.workspace_id)
            embedded = await self._runs.count_embedded_under_model(
                run.workspace_id, run.embedding_model_id
            )
            # Succeeded takes priority over a dead letter found in the same
            # pass: if every current document is already embedded under the
            # target model, the sweep achieved its goal regardless of a
            # since-superseded retry exhausting its budget.
            if embedded == current:
                await self._runs.mark_succeeded(run.run_id)
                continue
            if await self._runs.has_dead_lettered_sync_events(
                run.workspace_id, since=run.started_at
            ):
                await self._runs.mark_failed(run.run_id, error_code=DEAD_LETTERED_ERROR_CODE)
            # Otherwise: still running, nothing to do this pass.
