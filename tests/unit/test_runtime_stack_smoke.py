"""Proves the pinned runtime stack actually imports on the pinned Python.

Resolution is not the same as import. discord.py in particular imports the
stdlib ``audioop`` module at package import time, and ``audioop`` was removed in
Python 3.13 by PEP 594; upstream supplies the ``audioop-lts`` shim as a declared
dependency. This test is the cheap empirical check that the shim is in place and
that nothing else in the stack breaks on 3.14.
"""

from __future__ import annotations

import importlib
import sys

import pytest

RUNTIME_MODULES = [
    "alembic",
    "apscheduler",
    "discord",
    "fastapi",
    "httpx",
    "openai",
    "psycopg",
    "pydantic",
    "pydantic_settings",
    "sqlalchemy",
    "uvicorn",
]


def test_running_on_pinned_python_major_minor() -> None:
    assert sys.version_info[:2] == (3, 14), (
        f"expected Python 3.14 (see .python-version), got {sys.version.split()[0]}"
    )


@pytest.mark.parametrize("module_name", RUNTIME_MODULES)
def test_runtime_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)


def test_first_party_packages_import() -> None:
    for module_name in (
        "tc_domain",
        "tc_application",
        "tc_infrastructure",
        "tc_api",
        "tc_discord_bot",
        "tc_worker",
    ):
        importlib.import_module(module_name)
