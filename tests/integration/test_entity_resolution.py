"""Entity resolution and alias merging against real PostgreSQL (docs/DESIGN.md 6.4).

Runs as the least-privilege application role, the same role the organize
pipeline uses, so a missing grant fails here rather than in production.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.capture import ThoughtId, WorkspaceId
from tc_domain.entities import EntityResolutionThresholds, EntityType, MatchDecision
from tc_infrastructure.db.entity_reader import PostgresEntityReader
from tc_infrastructure.db.entity_repository import PostgresEntityRepository, ResolvedEntity
from tc_infrastructure.db.tables import runs, thoughts

pytestmark = pytest.mark.integration


@pytest.fixture
def workspace(seeded_identity: tuple[uuid.UUID, uuid.UUID]) -> WorkspaceId:
    return WorkspaceId(seeded_identity[0])


@pytest.fixture
def user_id(seeded_identity: tuple[uuid.UUID, uuid.UUID]) -> uuid.UUID:
    return seeded_identity[1]


@pytest.fixture
def unique(unique_message_id: str) -> str:
    """A short token that makes synthetic entity names collide only within one test.

    ``entities`` in the test database persists for the whole session (shared
    across every integration test file, like ``seeded_identity``), so two
    tests both saying "Jane Doe" would corrupt each other's trigram matches.
    """
    return unique_message_id.removeprefix("test-")[:8]


async def _run_row(
    factory: async_sessionmaker[AsyncSession], workspace_id: WorkspaceId
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            sa.insert(runs).values(
                id=run_id, workspace_id=workspace_id, kind="organize", status="running"
            )
        )
    return run_id


async def _thought_row(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: WorkspaceId,
    user_id: uuid.UUID,
    *,
    source_message_id: str,
) -> ThoughtId:
    now = dt.datetime.now(dt.UTC)
    async with factory() as session, session.begin():
        thought_id = await session.scalar(
            sa.insert(thoughts)
            .values(
                workspace_id=workspace_id,
                author_user_id=user_id,
                source="api",
                source_message_id=source_message_id,
                body="synthetic",
                client_created_at=now,
                client_timezone="Asia/Bangkok",
                client_local_date=now.date(),
                client_local_time=now.time(),
                content_language="en",
            )
            .returning(thoughts.c.id)
        )
    assert thought_id is not None
    return ThoughtId(thought_id)


async def _resolve(
    factory: async_sessionmaker[AsyncSession],
    repo: PostgresEntityRepository,
    **kwargs: object,
) -> ResolvedEntity:
    async with factory() as session, session.begin():
        return await repo.resolve_mention(session, **kwargs)  # type: ignore[arg-type]


async def test_a_new_entity_is_created_when_nothing_matches(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    run_id = await _run_row(app_session_factory, workspace)
    thought_id = await _thought_row(
        app_session_factory, workspace, user_id, source_message_id=unique
    )
    repo = PostgresEntityRepository()

    resolved = await _resolve(
        app_session_factory,
        repo,
        workspace_id=workspace,
        run_id=run_id,
        entity_type=EntityType.TOPIC,
        canonical_name=f"Zephyr Protocol {unique}",
        surface_form=f"Zephyr Protocol {unique}",
        confidence=0.9,
        thought_id=thought_id,
    )

    assert resolved.decision is MatchDecision.NEW
    assert resolved.stable_key.startswith("topic:zephyr-protocol")


async def test_an_identical_canonical_name_merges_into_the_existing_entity(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    repo = PostgresEntityRepository()
    run_id = await _run_row(app_session_factory, workspace)
    first_thought = await _thought_row(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-a"
    )
    second_thought = await _thought_row(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-b"
    )

    first = await _resolve(
        app_session_factory,
        repo,
        workspace_id=workspace,
        run_id=run_id,
        entity_type=EntityType.PERSON,
        canonical_name=f"Jane Rivera {unique}",
        surface_form=f"Jane Rivera {unique}",
        confidence=0.9,
        thought_id=first_thought,
    )
    second = await _resolve(
        app_session_factory,
        repo,
        workspace_id=workspace,
        run_id=run_id,
        entity_type=EntityType.PERSON,
        canonical_name=f"Jane Rivera {unique}",
        surface_form=f"Jane {unique}",  # a shorter surface form this time
        confidence=0.85,
        thought_id=second_thought,
    )

    assert second.decision is MatchDecision.MERGE
    assert second.entity_id == first.entity_id
    assert second.alias_added is True
    assert second.matched_similarity == pytest.approx(1.0)


async def test_a_repeated_surface_form_does_not_duplicate_the_alias(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    repo = PostgresEntityRepository()
    run_id = await _run_row(app_session_factory, workspace)

    async def resolve_once(tag: str) -> ResolvedEntity:
        thought_id = await _thought_row(
            app_session_factory, workspace, user_id, source_message_id=f"{unique}-{tag}"
        )
        return await _resolve(
            app_session_factory,
            repo,
            workspace_id=workspace,
            run_id=run_id,
            entity_type=EntityType.PROJECT,
            canonical_name=f"Aurora Launch {unique}",
            surface_form=f"Aurora {unique}",
            confidence=0.9,
            thought_id=thought_id,
        )

    first = await resolve_once("a")
    second = await resolve_once("b")

    assert first.alias_added is True
    assert second.alias_added is False, "the alias already existed after the first mention"
    assert second.entity_id == first.entity_id


async def test_an_ambiguous_match_stays_a_separate_entity(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    """Below the merge threshold, docs/DESIGN.md 6.4 forbids the automatic merge.

    Thresholds are tuned so that any partial overlap short of an exact match
    lands in the ambiguous band - the trigram score itself is not asserted,
    only the decision it produces.
    """
    repo = PostgresEntityRepository(
        thresholds=EntityResolutionThresholds(merge_at=0.999, ambiguous_floor=0.01)
    )
    run_id = await _run_row(app_session_factory, workspace)
    first_thought = await _thought_row(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-a"
    )
    second_thought = await _thought_row(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-b"
    )

    first = await _resolve(
        app_session_factory,
        repo,
        workspace_id=workspace,
        run_id=run_id,
        entity_type=EntityType.PLACE,
        canonical_name=f"Riverside Cafe {unique}",
        surface_form=f"Riverside Cafe {unique}",
        confidence=0.9,
        thought_id=first_thought,
    )
    second = await _resolve(
        app_session_factory,
        repo,
        workspace_id=workspace,
        run_id=run_id,
        entity_type=EntityType.PLACE,
        canonical_name=f"Riverside Diner {unique}",
        surface_form=f"Riverside Diner {unique}",
        confidence=0.9,
        thought_id=second_thought,
    )

    assert second.decision is MatchDecision.AMBIGUOUS
    assert second.entity_id != first.entity_id


async def test_resolve_mention_requires_exactly_one_anchor(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    unique: str,
) -> None:
    repo = PostgresEntityRepository()
    run_id = await _run_row(app_session_factory, workspace)

    with pytest.raises(ValueError, match="exactly one"):
        await _resolve(
            app_session_factory,
            repo,
            workspace_id=workspace,
            run_id=run_id,
            entity_type=EntityType.TOPIC,
            canonical_name=f"Neither {unique}",
            surface_form=f"Neither {unique}",
            confidence=0.9,
        )


async def test_the_reader_lists_aliases_and_mention_count(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    repo = PostgresEntityRepository()
    reader = PostgresEntityReader(app_session_factory)
    run_id = await _run_row(app_session_factory, workspace)

    first_thought = await _thought_row(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-a"
    )
    second_thought = await _thought_row(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-b"
    )

    first = await _resolve(
        app_session_factory,
        repo,
        workspace_id=workspace,
        run_id=run_id,
        entity_type=EntityType.ORGANIZATION,
        canonical_name=f"Northwind Cooperative {unique}",
        surface_form=f"Northwind Cooperative {unique}",
        confidence=0.9,
        thought_id=first_thought,
    )
    await _resolve(
        app_session_factory,
        repo,
        workspace_id=workspace,
        run_id=run_id,
        entity_type=EntityType.ORGANIZATION,
        canonical_name=f"Northwind Cooperative {unique}",
        surface_form=f"Northwind {unique}",
        confidence=0.9,
        thought_id=second_thought,
    )

    record = await reader.get(workspace, first.entity_id)
    assert record is not None
    assert record.mention_count == 2
    assert f"northwind {unique}".lower() in [a.lower() for a in record.aliases]
    assert record.last_mentioned_at is not None
    assert record.stable_key == first.stable_key


async def test_review_queue_surfaces_ambiguous_pairs(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    thresholds = EntityResolutionThresholds(merge_at=0.999, ambiguous_floor=0.01)
    repo = PostgresEntityRepository(thresholds=thresholds)
    reader = PostgresEntityReader(app_session_factory)
    run_id = await _run_row(app_session_factory, workspace)

    first_thought = await _thought_row(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-a"
    )
    second_thought = await _thought_row(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-b"
    )

    first = await _resolve(
        app_session_factory,
        repo,
        workspace_id=workspace,
        run_id=run_id,
        entity_type=EntityType.TOPIC,
        canonical_name=f"Quarterly Planning {unique}",
        surface_form=f"Quarterly Planning {unique}",
        confidence=0.9,
        thought_id=first_thought,
    )
    second = await _resolve(
        app_session_factory,
        repo,
        workspace_id=workspace,
        run_id=run_id,
        entity_type=EntityType.TOPIC,
        canonical_name=f"Quarterly Retro {unique}",
        surface_form=f"Quarterly Retro {unique}",
        confidence=0.9,
        thought_id=second_thought,
    )
    assert second.decision is MatchDecision.AMBIGUOUS

    queue = await reader.review_queue(workspace, thresholds=thresholds)
    pairs = {frozenset((c.entity_a, c.entity_b)) for c in queue}
    assert frozenset((first.entity_id, second.entity_id)) in pairs


async def test_review_queue_surfaces_an_alias_only_ambiguous_match(
    app_session_factory: async_sessionmaker[AsyncSession],
    workspace: WorkspaceId,
    user_id: uuid.UUID,
    unique: str,
) -> None:
    """A pair close only via one entity's *alias* must still reach the queue.

    ``resolve_mention`` matches against ``GREATEST(canonical, alias)``
    similarity (docs/DESIGN.md 6.4); the review queue has to use the same
    relation, or an alias-only ambiguous match becomes a permanent, silent
    duplicate that never surfaces for a human to merge.
    """
    thresholds = EntityResolutionThresholds(merge_at=0.999, ambiguous_floor=0.5)
    repo = PostgresEntityRepository(thresholds=thresholds)
    reader = PostgresEntityReader(app_session_factory)
    run_id = await _run_row(app_session_factory, workspace)

    unrelated_thought = await _thought_row(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-a"
    )
    other = await _resolve(
        app_session_factory,
        repo,
        workspace_id=workspace,
        run_id=run_id,
        entity_type=EntityType.TOPIC,
        canonical_name=f"Falcon Nine {unique}",
        surface_form=f"Falcon Nine {unique}",
        confidence=0.9,
        thought_id=unrelated_thought,
    )

    seed_thought = await _thought_row(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-b"
    )
    base = await _resolve(
        app_session_factory,
        repo,
        workspace_id=workspace,
        run_id=run_id,
        entity_type=EntityType.TOPIC,
        canonical_name=f"Willow Creek {unique}",
        surface_form=f"Willow Creek {unique}",
        confidence=0.9,
        thought_id=seed_thought,
    )
    assert base.entity_id != other.entity_id, "canonical names must not have auto-merged"

    # Attach an alias to ``base`` that is a near-exact match for ``other``'s
    # canonical name (missing one character) - close enough to land in the
    # ambiguous band, without being an exact match that would auto-merge.
    alias_thought = await _thought_row(
        app_session_factory, workspace, user_id, source_message_id=f"{unique}-c"
    )
    await _resolve(
        app_session_factory,
        repo,
        workspace_id=workspace,
        run_id=run_id,
        entity_type=EntityType.TOPIC,
        canonical_name=f"Willow Creek {unique}",
        surface_form=f"Falcon Nin {unique}",
        confidence=0.9,
        thought_id=alias_thought,
    )

    queue = await reader.review_queue(workspace, thresholds=thresholds)
    pairs = {frozenset((c.entity_a, c.entity_b)) for c in queue}
    assert frozenset((base.entity_id, other.entity_id)) in pairs, (
        "alias-only similarity must still surface the pair for review"
    )
