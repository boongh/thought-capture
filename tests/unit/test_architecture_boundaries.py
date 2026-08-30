"""Executable enforcement of the dependency direction in docs/DESIGN.md 5.3.

`domain` has no framework imports. `application` depends on domain ports only.
`infrastructure` implements the adapters. `apps/*` wires them together.

This is what keeps Discord replaceable and permits local models later, so it is
asserted in CI rather than left to review discipline.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Third-party packages that must never reach the inner layers.
FRAMEWORKS = frozenset(
    {
        "fastapi",
        "starlette",
        "uvicorn",
        "discord",
        "sqlalchemy",
        "psycopg",
        "psycopg2",
        "alembic",
        "openai",
        "httpx",
        "aiohttp",
        "apscheduler",
    }
)

FIRST_PARTY_LAYERS = frozenset(
    {
        "tc_domain",
        "tc_application",
        "tc_infrastructure",
        "tc_api",
        "tc_discord_bot",
        "tc_worker",
    }
)

# layer name -> (source root, modules it may not import)
LAYERS: dict[str, tuple[Path, frozenset[str]]] = {
    # The domain is stdlib-only. Pydantic is excluded too: it is a boundary
    # validation library, and the domain is not a boundary.
    "tc_domain": (
        REPO_ROOT / "packages" / "domain" / "src" / "tc_domain",
        FRAMEWORKS | {"pydantic", "pydantic_settings"} | (FIRST_PARTY_LAYERS - {"tc_domain"}),
    ),
    # The application layer orchestrates use cases against domain ports. It may
    # use pydantic for boundary and LLM structured-output schemas (DESIGN.md 7.3).
    "tc_application": (
        REPO_ROOT / "packages" / "application" / "src" / "tc_application",
        FRAMEWORKS | (FIRST_PARTY_LAYERS - {"tc_domain", "tc_application"}),
    ),
    # Infrastructure may use any driver, but must not depend on app wiring.
    "tc_infrastructure": (
        REPO_ROOT / "packages" / "infrastructure" / "src" / "tc_infrastructure",
        frozenset({"tc_api", "tc_discord_bot", "tc_worker"}),
    ),
}


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _imported_roots(source: str, filename: str) -> set[str]:
    """Return the top-level module name of every import in ``source``."""
    tree = ast.parse(source, filename=filename)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        # level > 0 is a relative import, which stays inside the package.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("layer", sorted(LAYERS))
def test_layer_does_not_import_forbidden_modules(layer: str) -> None:
    source_root, forbidden = LAYERS[layer]
    assert source_root.is_dir(), f"missing source root for {layer}: {source_root}"

    violations: list[str] = []
    for path in _python_files(source_root):
        for imported in sorted(_imported_roots(path.read_text(encoding="utf-8"), str(path))):
            if imported in forbidden:
                violations.append(f"{path.relative_to(REPO_ROOT).as_posix()} imports {imported!r}")

    assert not violations, (
        f"{layer} violates the dependency direction in docs/DESIGN.md 5.3:\n  "
        + "\n  ".join(violations)
    )


def test_domain_declares_no_runtime_dependencies() -> None:
    """The domain package must stay stdlib-only in its own metadata, too."""
    pyproject = (REPO_ROOT / "packages" / "domain" / "pyproject.toml").read_text(encoding="utf-8")
    # tomllib is stdlib; parse rather than pattern-match so formatting cannot fool us.
    import tomllib

    parsed = tomllib.loads(pyproject)
    dependencies = parsed["project"]["dependencies"]
    assert dependencies == [], (
        "packages/domain must declare no runtime dependencies (docs/DESIGN.md 5.3); "
        f"found: {dependencies}"
    )
