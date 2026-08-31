"""Guarantees of the derived layer created by migration 0004.

Derived documents are "versioned, reversible, and fully sourced" (CLAUDE.md).
Each of those three words is a property the database must enforce, not a habit
the pipeline is trusted to keep.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration

DERIVED_TABLES = {
    "runs",
    "documents",
    "document_revisions",
    "revision_sources",
    "entities",
    "entity_aliases",
    "entity_mentions",
    "capture_windows",
    "llm_calls",
    "run_context_selections",
}


def _run(
    connection: sa.Connection, workspace_id: uuid.UUID, *, kind: str = "organize"
) -> uuid.UUID:
    run_id = uuid.uuid4()
    connection.execute(
        sa.text("""
            INSERT INTO runs (id, workspace_id, kind, status)
            VALUES (:id, :workspace_id, :kind, 'succeeded')
        """),
        {"id": run_id, "workspace_id": workspace_id, "kind": kind},
    )
    return run_id


def _document(
    connection: sa.Connection, workspace_id: uuid.UUID, *, kind: str = "project"
) -> uuid.UUID:
    document_id = uuid.uuid4()
    connection.execute(
        sa.text("""
            INSERT INTO documents (id, workspace_id, kind, stable_key, title)
            VALUES (:id, :workspace_id, :kind, :stable_key, 'Synthetic')
        """),
        {
            "id": document_id,
            "workspace_id": workspace_id,
            "kind": kind,
            "stable_key": f"key-{uuid.uuid4()}",
        },
    )
    return document_id


def _revision(
    connection: sa.Connection,
    document_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    number: int,
    parent: uuid.UUID | None = None,
    body: str = "## Summary\n\nsynthetic",
    change_kind: str = "organize",
) -> uuid.UUID:
    revision_id = uuid.uuid4()
    connection.execute(
        sa.text("""
            INSERT INTO document_revisions (
              id, document_id, parent_revision_id, run_id, revision_number,
              body_markdown, body_sha256, change_summary, change_kind
            ) VALUES (
              :id, :document_id, :parent, :run_id, :number,
              :body, repeat('a', 64), 'synthetic change', :change_kind
            )
        """),
        {
            "id": revision_id,
            "document_id": document_id,
            "parent": parent,
            "run_id": run_id,
            "number": number,
            "body": body,
            "change_kind": change_kind,
        },
    )
    return revision_id


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_all_derived_tables_exist(connection: sa.Connection) -> None:
    present = {
        row[0]
        for row in connection.execute(
            sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
    }
    assert present >= DERIVED_TABLES, f"missing: {sorted(DERIVED_TABLES - present)}"


# ---------------------------------------------------------------------------
# Versioned and reversible
# ---------------------------------------------------------------------------


def test_a_revision_cannot_be_edited(connection: sa.Connection) -> None:
    """Full snapshots are immutable; reversion writes a new one (ADR-0004)."""
    workspace_id, _ = _seed(connection)
    run_id = _run(connection, workspace_id)
    document_id = _document(connection, workspace_id)
    revision_id = _revision(connection, document_id, run_id, number=1)

    with pytest.raises(sa.exc.DBAPIError, match="append-only"):
        connection.execute(
            sa.text("UPDATE document_revisions SET body_markdown = 'rewritten' WHERE id = :id"),
            {"id": revision_id},
        )


def test_a_revision_cannot_be_deleted(connection: sa.Connection) -> None:
    workspace_id, _ = _seed(connection)
    run_id = _run(connection, workspace_id)
    document_id = _document(connection, workspace_id)
    revision_id = _revision(connection, document_id, run_id, number=1)

    with pytest.raises(sa.exc.DBAPIError, match="append-only"):
        connection.execute(
            sa.text("DELETE FROM document_revisions WHERE id = :id"), {"id": revision_id}
        )


def test_revision_numbers_are_unique_per_document(connection: sa.Connection) -> None:
    workspace_id, _ = _seed(connection)
    run_id = _run(connection, workspace_id)
    document_id = _document(connection, workspace_id)
    _revision(connection, document_id, run_id, number=1)

    with pytest.raises(sa.exc.IntegrityError):
        _revision(connection, document_id, run_id, number=1)


def test_a_restore_appends_a_new_revision_pointing_at_the_current_one(
    connection: sa.Connection,
) -> None:
    """ "Reversion is forward motion" (docs/DESIGN.md 3.4).

    Restoring revision 1 writes revision 3 whose parent is 2 and whose body
    matches 1 - it does not resurrect or rewrite anything.
    """
    workspace_id, _ = _seed(connection)
    run_id = _run(connection, workspace_id)
    document_id = _document(connection, workspace_id)

    first = _revision(connection, document_id, run_id, number=1, body="original")
    second = _revision(connection, document_id, run_id, number=2, parent=first, body="changed")
    third = _revision(
        connection,
        document_id,
        run_id,
        number=3,
        parent=second,
        body="original",
        change_kind="manual_restore",
    )

    chain = connection.execute(
        sa.text("""
            SELECT revision_number, parent_revision_id, body_markdown, change_kind
            FROM document_revisions WHERE document_id = :id ORDER BY revision_number
        """),
        {"id": document_id},
    ).all()

    assert [row.revision_number for row in chain] == [1, 2, 3]
    assert chain[2].parent_revision_id == second
    assert chain[2].body_markdown == chain[0].body_markdown
    assert chain[2].change_kind == "manual_restore"
    assert third is not None


def test_current_revision_pointer_is_a_real_revision(connection: sa.Connection) -> None:
    workspace_id, _ = _seed(connection)
    document_id = _document(connection, workspace_id)

    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(
            sa.text("UPDATE documents SET current_revision_id = :missing WHERE id = :id"),
            {"missing": uuid.uuid4(), "id": document_id},
        )


# ---------------------------------------------------------------------------
# Fully sourced
# ---------------------------------------------------------------------------


def test_revision_sources_link_to_real_thoughts(connection: sa.Connection) -> None:
    """Provenance is a foreign key, so a citation cannot be invented."""
    workspace_id, _ = _seed(connection)
    run_id = _run(connection, workspace_id)
    document_id = _document(connection, workspace_id)
    revision_id = _revision(connection, document_id, run_id, number=1)

    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(
            sa.text("""
                INSERT INTO revision_sources (revision_id, thought_id, support_type)
                VALUES (:revision_id, 999999999, 'direct')
            """),
            {"revision_id": revision_id},
        )


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


def test_an_entity_is_unique_per_workspace_type_and_normalized_name(
    connection: sa.Connection,
) -> None:
    """This is what stops the same person becoming two documents."""
    workspace_id, _ = _seed(connection)
    values = {
        "workspace_id": workspace_id,
        "entity_type": "person",
        "canonical_name": "Alex Kim",
        "normalized_name": "alex kim",
    }
    statement = sa.text("""
        INSERT INTO entities (id, workspace_id, entity_type, canonical_name, normalized_name)
        VALUES (gen_random_uuid(), :workspace_id, :entity_type, :canonical_name, :normalized_name)
    """)
    connection.execute(statement, values)

    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(statement, values)


def test_a_mention_attaches_to_exactly_one_of_thought_or_revision(
    connection: sa.Connection,
) -> None:
    """The design's XOR check: a mention is in raw text or in a document, never both."""
    workspace_id, _ = _seed(connection)
    run_id = _run(connection, workspace_id)
    entity_id = uuid.uuid4()
    connection.execute(
        sa.text("""
            INSERT INTO entities (id, workspace_id, entity_type, canonical_name, normalized_name)
            VALUES (:id, :workspace_id, 'topic', 'Synthetic', 'synthetic')
        """),
        {"id": entity_id, "workspace_id": workspace_id},
    )

    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(
            sa.text("""
                INSERT INTO entity_mentions
                  (entity_id, thought_id, revision_id, run_id, surface_form, confidence)
                VALUES (:entity_id, NULL, NULL, :run_id, 'synthetic', 0.9)
            """),
            {"entity_id": entity_id, "run_id": run_id},
        )


# ---------------------------------------------------------------------------
# Capture windows
# ---------------------------------------------------------------------------


def test_a_window_must_end_after_it_starts(connection: sa.Connection) -> None:
    workspace_id, _ = _seed(connection)

    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(
            sa.text("""
                INSERT INTO capture_windows (workspace_id, window_start, window_end, status)
                VALUES (:workspace_id, '2026-08-31T13:00:00+00', '2026-08-30T13:00:00+00', 'closed')
            """),
            {"workspace_id": workspace_id},
        )


def test_a_window_end_is_unique_per_workspace(connection: sa.Connection) -> None:
    """Two runs must not be able to claim the same cutoff."""
    workspace_id, _ = _seed(connection)
    statement = sa.text("""
        INSERT INTO capture_windows (workspace_id, window_start, window_end, status)
        VALUES (:workspace_id, '2026-08-30T13:00:00+00', '2026-08-31T13:00:00+00', 'closed')
    """)
    connection.execute(statement, {"workspace_id": workspace_id})

    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(statement, {"workspace_id": workspace_id})


# ---------------------------------------------------------------------------
# Journals
# ---------------------------------------------------------------------------


def test_the_llm_journal_cannot_be_rewritten(connection: sa.Connection) -> None:
    """A journal that can be edited cannot reproduce anything (ADR-0008)."""
    workspace_id, _ = _seed(connection)
    run_id = _run(connection, workspace_id)
    call_id = uuid.uuid4()
    connection.execute(
        sa.text("""
            INSERT INTO llm_calls (
              id, workspace_id, run_id, step, sequence, prompt_version, schema_version,
              model_requested, request_messages, request_params
            ) VALUES (
              :id, :workspace_id, :run_id, 'organize', 1, 'v1', 'v1',
              'synthetic/model', '[]'::jsonb, '{}'::jsonb
            )
        """),
        {"id": call_id, "workspace_id": workspace_id, "run_id": run_id},
    )

    with pytest.raises(sa.exc.DBAPIError, match="append-only"):
        connection.execute(
            sa.text("UPDATE llm_calls SET model_requested = 'other' WHERE id = :id"),
            {"id": call_id},
        )


def test_a_run_may_record_context_recall_and_degradation(connection: sa.Connection) -> None:
    """docs/DESIGN.md 7.3.4 invariant 3 and 7.3.5."""
    workspace_id, _ = _seed(connection)
    run_id = _run(connection, workspace_id)

    connection.execute(
        sa.text("""
            UPDATE runs SET context_recall = 0.960, context_degraded = true WHERE id = :id
        """),
        {"id": run_id},
    )

    row = connection.execute(
        sa.text("SELECT context_recall, context_degraded FROM runs WHERE id = :id"),
        {"id": run_id},
    ).one()
    assert float(row.context_recall) == pytest.approx(0.96)
    assert row.context_degraded is True


def test_context_recall_must_be_a_proportion(connection: sa.Connection) -> None:
    workspace_id, _ = _seed(connection)
    run_id = _run(connection, workspace_id)

    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(
            sa.text("UPDATE runs SET context_recall = 1.5 WHERE id = :id"), {"id": run_id}
        )


def test_context_selection_records_signals_and_inclusion(connection: sa.Connection) -> None:
    """A document referenced while index_only is a retrieval miss (7.3.5)."""
    workspace_id, _ = _seed(connection)
    run_id = _run(connection, workspace_id)
    document_id = _document(connection, workspace_id)

    connection.execute(
        sa.text("""
            INSERT INTO run_context_selections
              (run_id, document_id, signals, inclusion, body_tokens, referenced_in_output)
            VALUES (:run_id, :document_id, ARRAY['alias','recency'], 'partial', 640, true)
        """),
        {"run_id": run_id, "document_id": document_id},
    )

    row = connection.execute(
        sa.text("""
            SELECT signals, inclusion, body_tokens, referenced_in_output
            FROM run_context_selections WHERE run_id = :run_id
        """),
        {"run_id": run_id},
    ).one()

    assert row.signals == ["alias", "recency"]
    assert row.inclusion == "partial"
    assert row.referenced_in_output is True


def test_inclusion_mode_is_constrained(connection: sa.Connection) -> None:
    workspace_id, _ = _seed(connection)
    run_id = _run(connection, workspace_id)
    document_id = _document(connection, workspace_id)

    with pytest.raises(sa.exc.IntegrityError):
        connection.execute(
            sa.text("""
                INSERT INTO run_context_selections
                  (run_id, document_id, signals, inclusion)
                VALUES (:run_id, :document_id, ARRAY['alias'], 'everything')
            """),
            {"run_id": run_id, "document_id": document_id},
        )


# ---------------------------------------------------------------------------
# Rebuild support
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["replay", "regenerate"])
def test_rebuild_run_kinds_are_accepted(connection: sa.Connection, kind: str) -> None:
    """ADR-0008 adds these two kinds to the design's list."""
    workspace_id, _ = _seed(connection)
    assert _run(connection, workspace_id, kind=kind) is not None


def test_a_replay_run_can_point_at_the_run_it_reproduces(connection: sa.Connection) -> None:
    workspace_id, _ = _seed(connection)
    original = _run(connection, workspace_id)
    replay = _run(connection, workspace_id, kind="replay")

    connection.execute(
        sa.text("UPDATE runs SET replay_of_run_id = :original WHERE id = :replay"),
        {"original": original, "replay": replay},
    )

    linked = connection.execute(
        sa.text("SELECT replay_of_run_id FROM runs WHERE id = :id"), {"id": replay}
    ).scalar_one()
    assert linked == original


def _seed(connection: sa.Connection) -> tuple[uuid.UUID, uuid.UUID]:
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()
    connection.execute(
        sa.text("INSERT INTO users (id, display_name) VALUES (:id, 'synthetic')"),
        {"id": user_id},
    )
    connection.execute(
        sa.text(
            "INSERT INTO workspaces (id, name, mode, timezone)"
            " VALUES (:id, 'synthetic', 'personal', 'Asia/Bangkok')"
        ),
        {"id": workspace_id},
    )
    return workspace_id, user_id
