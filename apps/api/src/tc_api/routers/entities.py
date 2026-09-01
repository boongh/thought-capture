"""Resolved entities and their aliases, read-only.

Alias-merge review (docs/DESIGN.md 6.4's ambiguous band) and any future
owner-approved merge action are out of scope for this gateway slice; this
router exposes only what entity resolution has already decided.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from tc_api.dependencies import Authenticated, Context
from tc_api.problems import not_found
from tc_api.schemas import EntityListResponse, EntityResponse
from tc_domain.entities import EntityType

router = APIRouter(prefix="/v1/entities", tags=["entities"], dependencies=[Authenticated])


@router.get(
    "", response_model=EntityListResponse, summary="Every entity, most recently created first"
)
async def list_entities(
    context: Context,
    entity_type: Annotated[EntityType | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> EntityListResponse:
    items = await context.entities.list_entities(
        context.workspace_id, entity_type=entity_type, limit=limit
    )
    return EntityListResponse.of(items)


@router.get(
    "/{entity_id}",
    response_model=EntityResponse,
    summary="One entity, its aliases, and mention count",
)
async def get_entity(entity_id: uuid.UUID, context: Context) -> EntityResponse:
    record = await context.entities.get(context.workspace_id, entity_id)
    if record is None:
        raise not_found(f"no entity {entity_id} in this workspace")
    return EntityResponse.of(record)
