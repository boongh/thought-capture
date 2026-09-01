"""Generated documents: entity documents and daily digests, read-only.

No POST/PUT/DELETE: documents are written only by the organize pipeline
(docs/DESIGN.md 7.2), never directly through this gateway.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from tc_api.dependencies import Authenticated, Context
from tc_api.problems import not_found
from tc_api.schemas import DocumentDetailResponse, DocumentListResponse
from tc_infrastructure.db.document_reader import DEFAULT_LIMIT, MAX_LIMIT

router = APIRouter(prefix="/v1/documents", tags=["documents"], dependencies=[Authenticated])


@router.get(
    "",
    response_model=DocumentListResponse,
    summary="Generated documents, most recently updated first",
)
async def list_documents(
    context: Context,
    kind: Annotated[
        str | None, Query(description="e.g. 'daily_digest', 'project', 'person'")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> DocumentListResponse:
    items = await context.documents.list_documents(context.workspace_id, kind=kind, limit=limit)
    return DocumentListResponse.of(items)


@router.get(
    "/{document_id}",
    response_model=DocumentDetailResponse,
    summary="One document's current revision and its source thoughts",
)
async def get_document(document_id: uuid.UUID, context: Context) -> DocumentDetailResponse:
    record = await context.documents.get_document(context.workspace_id, document_id)
    if record is None:
        raise not_found(f"no document {document_id} in this workspace")
    return DocumentDetailResponse.of(record)
