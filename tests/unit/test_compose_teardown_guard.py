"""Automated coverage for scripts/compose-teardown.sh and .ps1
(docs/incidents/0001-docker-compose-down-deleted-the-real-dev-stack.md).

Neither script had any automated test before this - only the manual
scenario runs recorded in commit messages. A Codex review of the incident's
own guardrail PR found a real gap that manual testing missed: Docker's flag
parser accepts `-v=true`/`--volumes=true`, not just the bare forms, and the
first version of these scripts let that through undetected. These tests are
what should have caught it, and now guard against the same class of miss for
every other accepted spelling.

Neither script is exercised against a real Docker daemon here - a fake
`docker` executable is put first on PATH, and every test asserts either "the
script refused before ever calling docker" (nothing appended to the call
log) or "the script called docker with exactly these trailing arguments".
That is what "refuses" and "passes through" mean for the purpose of these
scripts - they decide whether to invoke `docker compose`, not what Compose
itself then does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" and not shutil.which("bash"),
    reason="compose-teardown.sh requires a bash interpreter (Git Bash on Windows)",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BASH_SCRIPT = REPO_ROOT / "scripts" / "compose-teardown.sh"
PS_SCRIPT = REPO_ROOT / "scripts" / "compose-teardown.ps1"

REAL_PROJECT_NAME = "thought-capture"

# Every spelling Cobra/pflag (Docker's own flag parser) accepts for a boolean
# flag's explicit-false value - confirmed against real `docker compose down`
# in the PR that added this test file. Anything NOT in this list must be
# treated as volume-destroying (fail closed), including "0"-adjacent but
# unrecognized spellings like "no" or "off".
RECOGNIZED_FALSE_FORMS = ("0", "f", "F", "false", "False", "FALSE")

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


def _bash() -> str:
    found = shutil.which("bash")
    assert found, "bash is required to test scripts/compose-teardown.sh"
    return found


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
        # PATHEXT (which includes .cmd) - this is what scripts/compose-
        # teardown.ps1 actually goes through when run here.
        (stub_dir / "docker.cmd").write_text(DOCKER_STUB_CMD, newline="\r\n")
    else:
        # On Linux/macOS (this repo's own CI runs pwsh on ubuntu-latest),
        # PowerShell does NOT do Windows' PATHEXT-based extension
        # resolution for external commands - it only finds a bare `docker`
        # if a file literally named `docker` is on PATH and marked
        # executable. A `docker.cmd`-only stub dir is invisible to it there,
        # so every `docker` call fell through to whatever REAL `docker`
        # happened to be on the CI runner's PATH instead of this test's
        # stub - exactly the review finding this branch fixes (16
        # PowerShell pass-through tests failing on the Ubuntu runner,
        # because they were never actually exercising the stub at all).
        # Same content/log format as the Bash stub, since the Bash-target
        # stub script and this one need to behave identically here.
        docker_stub = stub_dir / "docker"
        docker_stub.write_text(DOCKER_STUB_SH, newline="\n")
        docker_stub.chmod(0o755)
    return stub_dir


def _run_sh(args: list[str], stub_dir: Path, log_file: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
    env["DOCKER_STUB_LOG"] = str(log_file)
    return subprocess.run(
        [_bash(), str(BASH_SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _run_ps(args: list[str], stub_dir: Path, log_file: Path) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    assert powershell, "a PowerShell interpreter is required to test scripts/compose-teardown.ps1"
    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
    env["DOCKER_STUB_LOG"] = str(log_file)
    return subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            # Windows PowerShell 5.1 defaults to a "Restricted" execution
            # policy that refuses to run ANY .ps1 file at all, including this
            # repository's own - Bypass here scopes that override to this one
            # subprocess invocation, not the developer's system-wide policy.
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PS_SCRIPT),
            *args,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _read_calls(log_file: Path) -> list[str]:
    if not log_file.exists():
        return []
    # .rstrip(): the .cmd stub's `echo %* >>file` (a space before the
    # redirect, itself required - see DOCKER_STUB_CMD's own comment) leaves
    # one trailing space before each line's newline.
    return [stripped for line in log_file.read_text().splitlines() if (stripped := line.rstrip())]


requires_powershell = pytest.mark.skipif(
    not (shutil.which("pwsh") or shutil.which("powershell")),
    reason="no PowerShell interpreter available",
)


# ---------------------------------------------------------------------------
# scripts/compose-teardown.sh
# ---------------------------------------------------------------------------


class TestBashGuard:
    def test_refuses_with_no_project_name(self, docker_stub_dir_sh: Path, tmp_path: Path) -> None:
        log = tmp_path / "calls.log"
        result = _run_sh(["down", "-v"], docker_stub_dir_sh, log)
        assert result.returncode == 1
        assert _read_calls(log) == []

    @pytest.mark.parametrize("volumes_flag", ["-v", "--volumes"])
    def test_refuses_bare_volumes_flag_against_the_real_project(
        self, docker_stub_dir_sh: Path, tmp_path: Path, volumes_flag: str
    ) -> None:
        log = tmp_path / "calls.log"
        result = _run_sh(["-p", REAL_PROJECT_NAME, "down", volumes_flag], docker_stub_dir_sh, log)
        assert result.returncode == 1
        assert _read_calls(log) == []

    @pytest.mark.parametrize("volumes_flag", ["-v=true", "--volumes=true", "-v=1", "--volumes=yes"])
    def test_refuses_equals_form_truthy_values(
        self, docker_stub_dir_sh: Path, tmp_path: Path, volumes_flag: str
    ) -> None:
        """The exact bypass Codex's review found: Docker's own flag parser
        accepts `-v=<value>`, not just the bare form. "yes" is deliberately
        included even though it is not one of pflag's own recognized
        spellings - failing closed means anything not explicitly falsy is
        treated as dangerous."""
        log = tmp_path / "calls.log"
        result = _run_sh(["-p", REAL_PROJECT_NAME, "down", volumes_flag], docker_stub_dir_sh, log)
        assert result.returncode == 1
        assert _read_calls(log) == []

    @pytest.mark.parametrize("false_form", RECOGNIZED_FALSE_FORMS)
    @pytest.mark.parametrize("flag_name", ["-v", "--volumes"])
    def test_passes_through_every_recognized_false_form(
        self, docker_stub_dir_sh: Path, tmp_path: Path, flag_name: str, false_form: str
    ) -> None:
        log = tmp_path / "calls.log"
        flag = f"{flag_name}={false_form}"
        result = _run_sh(["-p", REAL_PROJECT_NAME, "down", flag], docker_stub_dir_sh, log)
        assert result.returncode == 0, result.stderr
        calls = _read_calls(log)
        assert len(calls) == 1
        assert f"compose -p {REAL_PROJECT_NAME} down {flag}" == calls[0]

    def test_refuses_when_a_later_flag_downgrades_an_earlier_true(
        self, docker_stub_dir_sh: Path, tmp_path: Path
    ) -> None:
        """Duplicate/conflicting flags must only ever escalate toward
        refusal, never away from it - `docker compose` itself would honor
        whichever occurrence it parses last, but this wrapper cannot assume
        which one that is, so it must refuse if ANY occurrence was
        volume-destroying."""
        log = tmp_path / "calls.log"
        result = _run_sh(
            ["-p", REAL_PROJECT_NAME, "down", "-v", "--volumes=false"], docker_stub_dir_sh, log
        )
        assert result.returncode == 1
        assert _read_calls(log) == []

    def test_passes_through_when_a_later_flag_escalates_to_true(
        self, docker_stub_dir_sh: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "calls.log"
        result = _run_sh(
            ["-p", REAL_PROJECT_NAME, "down", "--volumes=false", "-v"], docker_stub_dir_sh, log
        )
        assert result.returncode == 1
        assert _read_calls(log) == []

    @pytest.mark.parametrize(
        "project_name_args",
        [
            ["-p", "tc-scratch-test"],
            ["--project-name", "tc-scratch-test"],
            ["-p=tc-scratch-test"],
            ["--project-name=tc-scratch-test"],
        ],
        ids=["-p value", "--project-name value", "-p=value", "--project-name=value"],
    )
    def test_recognizes_both_project_name_syntaxes_and_passes_through(
        self, docker_stub_dir_sh: Path, tmp_path: Path, project_name_args: list[str]
    ) -> None:
        """A different (non-real) project name with -v must pass through -
        proves both --project-name spellings are actually parsed into
        project_name, not merely accepted as unrecognized passthrough
        arguments that happen not to trigger the refusal by accident."""
        log = tmp_path / "calls.log"
        result = _run_sh([*project_name_args, "down", "-v"], docker_stub_dir_sh, log)
        assert result.returncode == 0, result.stderr
        assert len(_read_calls(log)) == 1

    @pytest.mark.parametrize(
        "project_name_args",
        [["-p", REAL_PROJECT_NAME], ["-p=" + REAL_PROJECT_NAME]],
        ids=["-p value", "-p=value"],
    )
    def test_both_project_name_syntaxes_still_refuse_against_the_real_project(
        self, docker_stub_dir_sh: Path, tmp_path: Path, project_name_args: list[str]
    ) -> None:
        log = tmp_path / "calls.log"
        result = _run_sh([*project_name_args, "down", "-v"], docker_stub_dir_sh, log)
        assert result.returncode == 1
        assert _read_calls(log) == []

    def test_passes_through_a_different_project_name_without_volumes(
        self, docker_stub_dir_sh: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "calls.log"
        result = _run_sh(["-p", REAL_PROJECT_NAME, "down"], docker_stub_dir_sh, log)
        assert result.returncode == 0, result.stderr
        assert _read_calls(log) == [f"compose -p {REAL_PROJECT_NAME} down"]


# ---------------------------------------------------------------------------
# scripts/compose-teardown.ps1 - same battery, same script logic ported to
# PowerShell. Skipped where no PowerShell interpreter is on PATH (this
# repository's own CI runs on ubuntu-latest, which ships pwsh; a developer
# machine without any PowerShell at all - unusual even on Linux/macOS with
# PowerShell Core installed - simply skips these instead of failing).
# ---------------------------------------------------------------------------


@requires_powershell
class TestPowerShellGuard:
    def test_refuses_with_no_project_name(self, docker_stub_dir_ps: Path, tmp_path: Path) -> None:
        log = tmp_path / "calls.log"
        result = _run_ps(["down", "-v"], docker_stub_dir_ps, log)
        assert result.returncode == 1
        assert _read_calls(log) == []

    @pytest.mark.parametrize("volumes_flag", ["-v", "--volumes"])
    def test_refuses_bare_volumes_flag_against_the_real_project(
        self, docker_stub_dir_ps: Path, tmp_path: Path, volumes_flag: str
    ) -> None:
        log = tmp_path / "calls.log"
        result = _run_ps(["-p", REAL_PROJECT_NAME, "down", volumes_flag], docker_stub_dir_ps, log)
        assert result.returncode == 1
        assert _read_calls(log) == []

    @pytest.mark.parametrize("volumes_flag", ["-v=true", "--volumes=true", "-v=1", "--volumes=yes"])
    def test_refuses_equals_form_truthy_values(
        self, docker_stub_dir_ps: Path, tmp_path: Path, volumes_flag: str
    ) -> None:
        log = tmp_path / "calls.log"
        result = _run_ps(["-p", REAL_PROJECT_NAME, "down", volumes_flag], docker_stub_dir_ps, log)
        assert result.returncode == 1
        assert _read_calls(log) == []

    @pytest.mark.parametrize("false_form", RECOGNIZED_FALSE_FORMS)
    @pytest.mark.parametrize("flag_name", ["-v", "--volumes"])
    def test_passes_through_every_recognized_false_form(
        self, docker_stub_dir_ps: Path, tmp_path: Path, flag_name: str, false_form: str
    ) -> None:
        log = tmp_path / "calls.log"
        flag = f"{flag_name}={false_form}"
        result = _run_ps(["-p", REAL_PROJECT_NAME, "down", flag], docker_stub_dir_ps, log)
        assert result.returncode == 0, result.stderr
        calls = _read_calls(log)
        assert len(calls) == 1
        assert calls[0] == f"compose -p {REAL_PROJECT_NAME} down {flag}"

    def test_refuses_when_a_later_flag_downgrades_an_earlier_true(
        self, docker_stub_dir_ps: Path, tmp_path: Path
    ) -> None:
        log = tmp_path / "calls.log"
        result = _run_ps(
            ["-p", REAL_PROJECT_NAME, "down", "-v", "--volumes=false"], docker_stub_dir_ps, log
        )
        assert result.returncode == 1
        assert _read_calls(log) == []

    @pytest.mark.parametrize(
        "project_name_args",
        [
            ["-p", "tc-scratch-test"],
            ["--project-name", "tc-scratch-test"],
            ["-p=tc-scratch-test"],
            ["--project-name=tc-scratch-test"],
        ],
        ids=["-p value", "--project-name value", "-p=value", "--project-name=value"],
    )
    def test_recognizes_both_project_name_syntaxes_and_passes_through(
        self, docker_stub_dir_ps: Path, tmp_path: Path, project_name_args: list[str]
    ) -> None:
        log = tmp_path / "calls.log"
        result = _run_ps([*project_name_args, "down", "-v"], docker_stub_dir_ps, log)
        assert result.returncode == 0, result.stderr
        assert len(_read_calls(log)) == 1

    @pytest.mark.parametrize(
        "project_name_args",
        [["-p", REAL_PROJECT_NAME], ["-p=" + REAL_PROJECT_NAME]],
        ids=["-p value", "-p=value"],
    )
    def test_both_project_name_syntaxes_still_refuse_against_the_real_project(
        self, docker_stub_dir_ps: Path, tmp_path: Path, project_name_args: list[str]
    ) -> None:
        log = tmp_path / "calls.log"
        result = _run_ps([*project_name_args, "down", "-v"], docker_stub_dir_ps, log)
        assert result.returncode == 1
        assert _read_calls(log) == []
