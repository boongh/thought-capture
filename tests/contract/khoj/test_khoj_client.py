"""``HttpKhojClient`` against a real, pinned Khoj instance (docs/adr/0003).

Every test uses a distinct query string built from `unique` - docs/adr/0003's
contract-spike finding 3 confirmed Khoj caches identical query strings, which
would otherwise let a stale cached response pass a test for the wrong reason.
"""

from __future__ import annotations

import pytest

from tc_domain.khoj_ports import KhojIndexFile
from tc_infrastructure.khoj.client import HttpKhojClient

pytestmark = pytest.mark.contract


def _file(unique: str, *, body: str | None = None) -> KhojIndexFile:
    content = body or f"# Test document {unique}\n\nBody mentions {unique} once."
    return KhojIndexFile(
        filename=f"contract-test/{unique}.md",
        content=content.encode("utf-8"),
    )


async def test_indexed_content_becomes_searchable(khoj_client: HttpKhojClient, unique: str) -> None:
    await khoj_client.index((_file(unique),))

    results = await khoj_client.search(unique)

    assert any(unique in r.entry for r in results)


async def test_search_result_echoes_the_uploaded_filename(
    khoj_client: HttpKhojClient, unique: str
) -> None:
    file = _file(unique)
    await khoj_client.index((file,))

    results = await khoj_client.search(unique)

    assert any(r.filename == file.filename for r in results)


async def test_deleted_content_is_no_longer_searchable(
    khoj_client: HttpKhojClient, unique: str
) -> None:
    file = _file(unique)
    await khoj_client.index((file,))
    results_before = await khoj_client.search(unique)
    assert any(r.filename == file.filename for r in results_before)

    await khoj_client.delete((file.filename,))

    # A *different* query text than the one used to confirm indexing above -
    # docs/adr/0003 finding 3: Khoj caches identical query strings, so
    # repeating `unique` alone here could return a stale cached hit rather
    # than proving the delete actually took effect.
    results_after = await khoj_client.search(f"{unique} deleted-check")
    assert all(r.filename != file.filename for r in results_after)


async def test_index_is_idempotent_for_the_same_filename(
    khoj_client: HttpKhojClient, unique: str
) -> None:
    """Re-indexing the same filename replaces its content rather than duplicating it -
    the property the index-sync use case (slice 18) depends on for a document
    whose current revision changes."""
    first = _file(unique, body=f"# {unique}\n\noriginal-marker-{unique}")
    await khoj_client.index((first,))

    second = _file(unique, body=f"# {unique}\n\nreplaced-marker-{unique}")
    await khoj_client.index((second,))

    results = await khoj_client.search(f"replaced-marker-{unique}")
    matches = [r for r in results if r.filename == first.filename]
    assert matches
    assert all("original-marker" not in r.entry for r in matches)
