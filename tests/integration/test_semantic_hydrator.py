"""``PostgresSemanticHydrator`` against real organize-writer output
(docs/DESIGN.md 7.5, 9.2). Reuses ``test_search_reader.py``'s document-seeding
helpers and fixtures rather than duplicating them - both files build the same
kind of fixture data over the same schema.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import WorkspaceId
from tc_domain.khoj_export import khoj_filename
from tc_infrastructure.db.semantic_hydrator import PostgresSemanticHydrator
from tests.integration.test_search_reader import (
    _thought_id,
    _write_document,
    unique,
    user_id,
    workspace,
)

pytestmark = pytest.mark.integration

__all__ = ["unique", "user_id", "workspace"]  # re-exported fixtures


async def test_hydrate_resolves_a_filename_to_its_current_revision(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    thought_id = await _thought_id(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    document_id, revision_id = await _write_document(
        app_session_factory,
        workspace,
        thought_id,
        stable_key=f"project:hydrate-{unique}",
        kind="project",
        title=f"Hydrate {unique}",
        body_markdown="## Summary\n\nsomething distinctive",
    )
    filename = khoj_filename(
        workspace_id=workspace,
        kind="project",
        stable_key=f"project:hydrate-{unique}",
        document_id=document_id,
    )

    hydrator = PostgresSemanticHydrator(app_session_factory)
    hydrated = await hydrator.hydrate(workspace, (filename,))

    assert filename in hydrated
    result = hydrated[filename]
    assert result.document_id == document_id
    assert result.revision_id == revision_id
    assert result.channels == ("semantic",)
    assert result.thought_ids == (thought_id,)


async def test_hydrate_skips_a_filename_from_a_different_workspace(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
) -> None:
    foreign_filename = khoj_filename(
        workspace_id=uuid.uuid4(),
        kind="project",
        stable_key="project:foreign",
        document_id=uuid.uuid4(),
    )

    hydrator = PostgresSemanticHydrator(app_session_factory)
    hydrated = await hydrator.hydrate(workspace, (foreign_filename,))

    assert hydrated == {}


async def test_hydrate_skips_an_unparseable_filename(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
) -> None:
    hydrator = PostgresSemanticHydrator(app_session_factory)
    hydrated = await hydrator.hydrate(workspace, ("not-a-khoj-filename.md",))

    assert hydrated == {}


async def test_hydrate_skips_a_document_id_that_does_not_resolve(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
) -> None:
    """A well-formed filename for a document that was never written (or
    belongs to a workspace's stale export) must not raise."""
    phantom_filename = khoj_filename(
        workspace_id=workspace,
        kind="project",
        stable_key="project:phantom",
        document_id=uuid.uuid4(),
    )

    hydrator = PostgresSemanticHydrator(app_session_factory)
    hydrated = await hydrator.hydrate(workspace, (phantom_filename,))

    assert hydrated == {}
