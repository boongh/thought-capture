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
        already stored - the out-of-order-redelivery guard: an at-least-once,
        out-of-order outbox redelivery must never overwrite a newer embedding
        with an older one.

        The guard compares the incoming ``revision_number`` against the
        stored row's own revision's number, looked up via a correlated
        subquery against ``document_revisions`` keyed on the stored
        ``revision_id`` - ``document_embeddings`` itself carries no
        ``revision_number`` column to compare directly.
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
        guard = sa.text(
            "(SELECT revision_number FROM document_revisions "
            "WHERE id = document_embeddings.revision_id) < :incoming_revision_number"
        ).bindparams(incoming_revision_number=revision_number)

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
