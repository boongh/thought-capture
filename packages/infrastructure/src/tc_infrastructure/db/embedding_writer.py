"""``document_embeddings`` upsert (docs/DESIGN.md 8.4's write-path guard,
ADR-0010 §3).

One statement, modeled exactly on
``tests/integration/test_document_embeddings.py::test_upsert_by_document_id_replaces_the_prior_revision``'s
``INSERT ... ON CONFLICT (document_id) DO UPDATE`` shape - including setting
``updated_at`` explicitly in the ``DO UPDATE``, since its ``DEFAULT now()``
only fires on ``INSERT``.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tc_domain.embedding_ports import EmbeddingVector
from tc_infrastructure.db.tables import document_embeddings


class PostgresEmbeddingWriter:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert(
        self,
        *,
        workspace_id: uuid.UUID,
        document_id: uuid.UUID,
        revision_id: uuid.UUID,
        revision_number: int,
        vector: EmbeddingVector,
    ) -> bool:
        """Returns ``False`` (no write) when a strictly-newer revision is
        already stored, or when the stored row is already the *same*
        revision embedded by the *same* model - the out-of-order-redelivery
        guard: an at-least-once, out-of-order outbox redelivery must never
        overwrite a newer embedding with an older one, and a redundant
        redelivery of a revision already embedded by the current model must
        be a no-op rather than rewriting an identical vector.

        The guard compares the incoming ``revision_number`` against the
        stored row's own revision's number, looked up via a correlated
        subquery against ``document_revisions`` keyed on the stored
        ``revision_id`` - ``document_embeddings`` itself carries no
        ``revision_number`` column to compare directly. A same-revision
        write is additionally admitted when ``embedding_model_id`` differs,
        so a forced re-embed sweep (docs/DESIGN.md 8.5) after an embedding
        model change can replace a vector without the document's revision
        having moved.
        """
        # Raw SQL text for just this fragment, deliberately not a SQLAlchemy
        # ORM subquery: `ON CONFLICT DO UPDATE ... WHERE` is not a SELECT, so
        # SQLAlchemy has no enclosing query to correlate `document_embeddings`
        # against - `.correlate()` is a no-op here. Left as an ORM
        # `.scalar_subquery()`, `document_embeddings` gets pulled into the
        # subquery's own FROM clause instead of referring to the row being
        # conflicted into, turning the guard into an uncorrelated cross join
        # over every stored row (CardinalityViolation the moment more than
        # one document_embeddings row exists) instead of a check against the
        # single row this statement is updating. In plain Postgres SQL,
        # referencing the target table's own name (unqualified, not listed in
        # the subquery's FROM) inside ON CONFLICT DO UPDATE correctly resolves
        # to the pre-existing row - the same way `excluded.column` resolves to
        # the incoming row - so writing it as text is the correct fix, not a
        # workaround.
        #
        # The second (OR'd) branch admits a same-revision write when the
        # stored row's `embedding_model_id` differs from the incoming one -
        # this is what lets a forced re-embed sweep replace every vector
        # after an operator swaps the embedding model, even though no
        # document's revision moved. It is deliberately model-gated rather
        # than an unconditional `<=`: a stale, out-of-order redelivery of an
        # *older* revision under the *same* model still fails both branches
        # (equal-revision check requires equality, and it isn't), so it
        # stays rejected exactly as before. Only "same revision, different
        # model" is newly admitted - "same revision, same model" still
        # returns `False`, so a forced sync that is re-run after partial
        # completion is cheap: already-migrated rows are skipped, not
        # rewritten with an identical vector.
        guard = sa.text(
            "(SELECT revision_number FROM document_revisions "
            "WHERE id = document_embeddings.revision_id) < :incoming_revision_number "
            "OR ("
            "(SELECT revision_number FROM document_revisions "
            "WHERE id = document_embeddings.revision_id) = :incoming_revision_number "
            "AND document_embeddings.embedding_model_id IS DISTINCT FROM :incoming_model_id"
            ")"
        ).bindparams(
            incoming_revision_number=revision_number,
            incoming_model_id=vector.model_id,
        )

        insert_statement = pg_insert(document_embeddings).values(
            document_id=document_id,
            revision_id=revision_id,
            embedding=list(vector.values),
            embedding_model_id=vector.model_id,
        )
        statement = insert_statement.on_conflict_do_update(
            index_elements=[document_embeddings.c.document_id],
            set_={
                "revision_id": insert_statement.excluded.revision_id,
                "embedding": insert_statement.excluded.embedding,
                "embedding_model_id": insert_statement.excluded.embedding_model_id,
                "updated_at": sa.func.now(),
            },
            where=guard,
        ).returning(document_embeddings.c.document_id)

        async with self._session_factory() as session, session.begin():
            result = await session.execute(statement)
            return result.first() is not None
