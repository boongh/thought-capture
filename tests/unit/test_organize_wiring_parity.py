"""The worker and the Discord bot must wire the same ``OrganizeWindow``.

A manual `/organize` and a scheduled cutoff run the exact same use case
(``tc_application.organize.OrganizeWindow``) - there has never been a second
implementation. But each entrypoint builds its own instance, so a keyword
argument added to one composition root and not the other silently changes
what a manual run actually does, with no import error to catch it: this is
exactly what happened when PR #16 added `select_provider` to
`tc_worker.__main__` and `tc_discord_bot.__main__` (built from an
already-diverged branch) kept passing only `provider`, so `/organize`'s
context-assembly "select" call silently fell back to the unreviewed organize
provider instead of the reviewed select-stage model.

Parsed with ``ast`` rather than imported and run, matching
``test_architecture_boundaries.py``'s existing style: constructing either
entrypoint's ``serve()`` needs a live database and Discord/OpenRouter
credentials, which a unit test must not require.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

WORKER_MAIN = REPO_ROOT / "apps" / "worker" / "src" / "tc_worker" / "__main__.py"
BOT_MAIN = REPO_ROOT / "apps" / "discord_bot" / "src" / "tc_discord_bot" / "__main__.py"


def _organize_window_call_keywords(path: Path) -> set[str]:
    """The keyword-argument names passed to the single ``OrganizeWindow(...)`` call."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "OrganizeWindow"
    ]
    assert len(calls) == 1, (
        f"expected exactly one OrganizeWindow(...) call in {path}, found {len(calls)}"
    )
    return {kw.arg for kw in calls[0].keywords if kw.arg is not None}


def test_worker_and_bot_pass_the_same_organize_window_keywords() -> None:
    worker_keywords = _organize_window_call_keywords(WORKER_MAIN)
    bot_keywords = _organize_window_call_keywords(BOT_MAIN)

    assert worker_keywords == bot_keywords, (
        "tc_worker.__main__ and tc_discord_bot.__main__ build OrganizeWindow with "
        f"different keyword arguments - worker only: {worker_keywords - bot_keywords}, "
        f"bot only: {bot_keywords - worker_keywords}. A scheduled run and a manual "
        "/organize must configure the identical pipeline."
    )


def test_bot_wires_a_dedicated_select_provider() -> None:
    """Regression test for the exact gap PR #20's review caught: `select_provider`
    must be passed explicitly, not left to OrganizeWindow's `provider` fallback."""
    assert "select_provider" in _organize_window_call_keywords(BOT_MAIN)
