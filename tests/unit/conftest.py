"""Shared fixtures for the unit tests that exercise this repository's shell and
PowerShell wrapper scripts.

Those tests never touch a real Docker daemon: a fake `docker` executable goes
first on PATH and each test asserts either "the script refused before ever
calling docker" (nothing appended to the call log) or "the script called docker
with exactly these arguments". These stubs started life inside
test_compose_teardown_guard.py and moved here when
test_backup_project_pinning.py needed the same harness - the comments below
record two platform traps that were found the hard way, and one copy of them is
the point.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

DOCKER_STUB_SH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DOCKER_STUB_LOG"
exit 0
"""

DOCKER_STUB_CMD = """@echo off
rem A space before the redirect operator is required: cmd.exe treats a
rem digit immediately adjacent to `>>` as a stream-handle number rather
rem than data - `echo %*>>file` silently ate a trailing "0" argument
rem (e.g. "-v=0" becoming "-v=") until this was found by testing that
rem exact case against the real compose-teardown.ps1.
echo %* >>"%DOCKER_STUB_LOG%"
exit /b 0
"""


@pytest.fixture
def docker_stub_dir_sh(tmp_path: Path) -> Path:
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    docker_stub = stub_dir / "docker"
    docker_stub.write_text(DOCKER_STUB_SH, newline="\n")
    docker_stub.chmod(0o755)
    return stub_dir


@pytest.fixture
def docker_stub_dir_ps(tmp_path: Path) -> Path:
    stub_dir = tmp_path / "stub-bin-ps"
    stub_dir.mkdir()
    if sys.platform == "win32":
        # Windows PowerShell/pwsh resolves a bare `docker` invocation through
        # PATHEXT (which includes .cmd) - this is what the .ps1 scripts under
        # test actually go through when run here.
        (stub_dir / "docker.cmd").write_text(DOCKER_STUB_CMD, newline="\r\n")
    else:
        # On Linux/macOS (this repo's own CI runs pwsh on ubuntu-latest),
        # PowerShell does NOT do Windows' PATHEXT-based extension resolution
        # for external commands - it only finds a bare `docker` if a file
        # literally named `docker` is on PATH and marked executable. A
        # `docker.cmd`-only stub dir is invisible to it there, so every
        # `docker` call fell through to whatever REAL `docker` happened to be
        # on the CI runner's PATH instead of this test's stub - a review
        # finding on PR #32 (16 PowerShell pass-through tests failing on the
        # Ubuntu runner, because they were never actually exercising the stub
        # at all). Same content/log format as the Bash stub, since the two need
        # to behave identically here.
        docker_stub = stub_dir / "docker"
        docker_stub.write_text(DOCKER_STUB_SH, newline="\n")
        docker_stub.chmod(0o755)
    return stub_dir
