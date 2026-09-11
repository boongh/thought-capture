"""Automated coverage for the Compose project scripts/backup.sh and
scripts/backup.ps1 target (PR #32 review finding, P1).

Both wrappers originally ran `docker compose` with no `-p`, relying on
deploy/compose/docker-compose.yml's own `name: thought-capture`. Compose
resolves a project name `-p` > COMPOSE_PROJECT_NAME (ambient, or read from the
`--env-file`) > the file's `name:`, so an operator with that variable set for a
second stack got a green, successful backup of a different - probably empty -
stack. A backup that silently captures the wrong data is the worst failure this
slice can have, so the wrappers now pin `-p thought-capture` on every call and
refuse outright when an override disagrees.

Neither script is exercised against a real Docker daemon: a fake `docker` goes
first on PATH (the stubs live in tests/unit/conftest.py, shared with
test_compose_teardown_guard.py), and each test asserts either "refused before
ever calling docker" - an empty call log - or "called docker with the pinned
project name".

Every case runs a byte-for-byte COPY of the script under test from a throwaway
directory, never the real checkout: the wrappers `cd` to their own parent's
parent and read `.env` from there, one case needs a `.env` that sets
COMPOSE_PROJECT_NAME, and the operator's real `.env` must never be read,
written, or depended on by a test run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" and not shutil.which("bash"),
    reason="scripts/backup.sh requires a bash interpreter (Git Bash on Windows)",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BASH_SCRIPT = REPO_ROOT / "scripts" / "backup.sh"
PS_SCRIPT = REPO_ROOT / "scripts" / "backup.ps1"
COMPOSE_TEARDOWN_SH = REPO_ROOT / "scripts" / "compose-teardown.sh"
COMPOSE_TEARDOWN_PS1 = REPO_ROOT / "scripts" / "compose-teardown.ps1"
DOCKER_COMPOSE_YML = REPO_ROOT / "deploy" / "compose" / "docker-compose.yml"

REAL_PROJECT_NAME = "thought-capture"
OTHER_PROJECT_NAME = "tc-some-other-stack"

# Enough for Compose to be handed a plausible env file; the stub never reads it.
MINIMAL_ENV_FILE = "POSTGRES_PASSWORD=synthetic-not-a-real-secret\n"

requires_powershell = pytest.mark.skipif(
    not (shutil.which("pwsh") or shutil.which("powershell")),
    reason="no PowerShell interpreter available",
)


def _fake_repo(tmp_path: Path, script: Path, env_file_text: str | None) -> Path:
    """A throwaway repo root holding a copy of `script` under `scripts/`."""
    repo = tmp_path / f"fake-repo-{script.stem}-{script.suffix.lstrip('.')}"
    (repo / "scripts").mkdir(parents=True)
    copied = repo / "scripts" / script.name
    copied.write_bytes(script.read_bytes())
    copied.chmod(0o755)
    if env_file_text is not None:
        (repo / ".env").write_bytes(env_file_text.encode("utf-8"))
    return repo


def _base_env(stub_dir: Path, log_file: Path, extra: dict[str, str] | None) -> dict[str, str]:
    env = dict(os.environ)
    # Popped, not merely overwritten: the developer running these tests may
    # legitimately have it exported, and the clean-environment cases must mean
    # what they say.
    env.pop("COMPOSE_PROJECT_NAME", None)
    env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
    env["DOCKER_STUB_LOG"] = str(log_file)
    if extra:
        env.update(extra)
    return env


def _run_sh(
    repo: Path, stub_dir: Path, log_file: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash, "bash is required to test scripts/backup.sh"
    # `.as_posix()`, not `str(...)`: on Windows, backup.sh's own directory
    # detection (`${BASH_SOURCE[0]%/*}`) only strips a trailing `/`-delimited
    # segment. Handed a raw backslash path - which is exactly what `str()` on
    # a WindowsPath produces, and MSYS bash does NOT rewrite into a `/c/...`
    # form for a plain script-path argument - that pattern match fails, the
    # script falls back to cd-ing to "." instead of its own real directory,
    # and the wrapper then looks for `.env` relative to the wrong directory
    # and silently skips the override check this test exists to exercise. A
    # real invocation (`scripts/backup.sh`, `./scripts/backup.sh`, or any
    # POSIX shell) never hits this - only a raw Windows path handed straight
    # to bash does.
    return subprocess.run(
        [bash, (repo / "scripts" / "backup.sh").as_posix()],
        env=_base_env(stub_dir, log_file, extra_env),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _run_ps(
    repo: Path, stub_dir: Path, log_file: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    assert powershell, "a PowerShell interpreter is required to test scripts/backup.ps1"
    return subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            # Windows PowerShell 5.1 defaults to a "Restricted" execution
            # policy that refuses to run ANY .ps1 file; Bypass scopes that
            # override to this one subprocess, not the developer's system.
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(repo / "scripts" / "backup.ps1"),
        ],
        env=_base_env(stub_dir, log_file, extra_env),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _read_calls(log_file: Path) -> list[str]:
    if not log_file.exists():
        return []
    # .rstrip(): the .cmd stub's `echo %* >>file` leaves one trailing space.
    return [stripped for line in log_file.read_text().splitlines() if (stripped := line.rstrip())]


def _assert_pinned(calls: list[str]) -> None:
    assert len(calls) == 2, f"expected the postgres warm-up and the backup run, got: {calls}"
    for call in calls:
        assert f"-p {REAL_PROJECT_NAME}" in call, (
            f"every `docker compose` call a backup makes must pin the real project "
            f"name explicitly, got: {call}"
        )


class TestBashWrapper:
    def test_pins_the_real_project_name_on_every_docker_call(
        self, docker_stub_dir_sh: Path, tmp_path: Path
    ) -> None:
        repo = _fake_repo(tmp_path, BASH_SCRIPT, MINIMAL_ENV_FILE)
        log = tmp_path / "calls.log"
        result = _run_sh(repo, docker_stub_dir_sh, log)
        assert result.returncode == 0, result.stderr
        _assert_pinned(_read_calls(log))

    def test_pins_the_real_project_name_with_no_env_file_at_all(
        self, docker_stub_dir_sh: Path, tmp_path: Path
    ) -> None:
        repo = _fake_repo(tmp_path, BASH_SCRIPT, None)
        log = tmp_path / "calls.log"
        result = _run_sh(repo, docker_stub_dir_sh, log)
        assert result.returncode == 0, result.stderr
        _assert_pinned(_read_calls(log))

    def test_refuses_an_ambient_project_name_override(
        self, docker_stub_dir_sh: Path, tmp_path: Path
    ) -> None:
        repo = _fake_repo(tmp_path, BASH_SCRIPT, MINIMAL_ENV_FILE)
        log = tmp_path / "calls.log"
        result = _run_sh(
            repo, docker_stub_dir_sh, log, {"COMPOSE_PROJECT_NAME": OTHER_PROJECT_NAME}
        )
        assert result.returncode == 1
        assert _read_calls(log) == [], "must refuse before starting or touching any stack"
        assert "COMPOSE_PROJECT_NAME" in result.stderr

    def test_refuses_an_env_file_project_name_override(
        self, docker_stub_dir_sh: Path, tmp_path: Path
    ) -> None:
        repo = _fake_repo(tmp_path, BASH_SCRIPT, f"COMPOSE_PROJECT_NAME={OTHER_PROJECT_NAME}\n")
        log = tmp_path / "calls.log"
        result = _run_sh(repo, docker_stub_dir_sh, log)
        assert result.returncode == 1
        assert _read_calls(log) == []
        assert OTHER_PROJECT_NAME in result.stderr

    def test_refuses_a_quoted_crlf_env_file_override(
        self, docker_stub_dir_sh: Path, tmp_path: Path
    ) -> None:
        """A .env edited on Windows carries CRLF, and quoting the value is
        legal - neither may let an override slip past the check."""
        repo = _fake_repo(tmp_path, BASH_SCRIPT, f'COMPOSE_PROJECT_NAME="{OTHER_PROJECT_NAME}"\r\n')
        log = tmp_path / "calls.log"
        result = _run_sh(repo, docker_stub_dir_sh, log)
        assert result.returncode == 1
        assert _read_calls(log) == []

    def test_allows_an_override_that_agrees_with_the_real_project_name(
        self, docker_stub_dir_sh: Path, tmp_path: Path
    ) -> None:
        repo = _fake_repo(tmp_path, BASH_SCRIPT, MINIMAL_ENV_FILE)
        log = tmp_path / "calls.log"
        result = _run_sh(repo, docker_stub_dir_sh, log, {"COMPOSE_PROJECT_NAME": REAL_PROJECT_NAME})
        assert result.returncode == 0, result.stderr
        _assert_pinned(_read_calls(log))

    def test_allows_an_env_file_value_that_agrees_with_the_real_project_name(
        self, docker_stub_dir_sh: Path, tmp_path: Path
    ) -> None:
        repo = _fake_repo(tmp_path, BASH_SCRIPT, f"COMPOSE_PROJECT_NAME={REAL_PROJECT_NAME}\n")
        log = tmp_path / "calls.log"
        result = _run_sh(repo, docker_stub_dir_sh, log)
        assert result.returncode == 0, result.stderr
        _assert_pinned(_read_calls(log))

    def test_allows_a_quoted_crlf_env_file_value_that_agrees_with_the_real_project_name(
        self, docker_stub_dir_sh: Path, tmp_path: Path
    ) -> None:
        """The CRLF-refusal test above only proves a MISMATCHED CRLF value is
        refused, which would happen anyway on name inequality alone regardless
        of whether CR-stripping does anything. This proves the positive case:
        a CRLF-carrying, quoted value that agrees with the real project name
        must be genuinely recognized as agreeing, not accidentally treated as
        a mismatch because a stray `\\r` was left attached to it. (The
        pipeline strips a trailing `\\r` twice over - explicitly via `tr -d
        '\\r'`, and again incidentally via the trailing-whitespace sed further
        down, since `[[:space:]]` includes CR - so this deliberately checks
        the outcome rather than pinning down which stage does the stripping.)
        """
        repo = _fake_repo(tmp_path, BASH_SCRIPT, f'COMPOSE_PROJECT_NAME="{REAL_PROJECT_NAME}"\r\n')
        log = tmp_path / "calls.log"
        result = _run_sh(repo, docker_stub_dir_sh, log)
        assert result.returncode == 0, result.stderr
        _assert_pinned(_read_calls(log))


@requires_powershell
class TestPowerShellWrapper:
    def test_pins_the_real_project_name_on_every_docker_call(
        self, docker_stub_dir_ps: Path, tmp_path: Path
    ) -> None:
        repo = _fake_repo(tmp_path, PS_SCRIPT, MINIMAL_ENV_FILE)
        log = tmp_path / "calls-ps.log"
        result = _run_ps(repo, docker_stub_dir_ps, log)
        assert result.returncode == 0, result.stdout + result.stderr
        _assert_pinned(_read_calls(log))

    def test_refuses_an_ambient_project_name_override(
        self, docker_stub_dir_ps: Path, tmp_path: Path
    ) -> None:
        repo = _fake_repo(tmp_path, PS_SCRIPT, MINIMAL_ENV_FILE)
        log = tmp_path / "calls-ps.log"
        result = _run_ps(
            repo, docker_stub_dir_ps, log, {"COMPOSE_PROJECT_NAME": OTHER_PROJECT_NAME}
        )
        assert result.returncode == 1
        assert _read_calls(log) == []

    def test_refuses_an_env_file_project_name_override(
        self, docker_stub_dir_ps: Path, tmp_path: Path
    ) -> None:
        repo = _fake_repo(tmp_path, PS_SCRIPT, f"COMPOSE_PROJECT_NAME={OTHER_PROJECT_NAME}\n")
        log = tmp_path / "calls-ps.log"
        result = _run_ps(repo, docker_stub_dir_ps, log)
        assert result.returncode == 1
        assert _read_calls(log) == []

    def test_allows_an_override_that_agrees_with_the_real_project_name(
        self, docker_stub_dir_ps: Path, tmp_path: Path
    ) -> None:
        repo = _fake_repo(tmp_path, PS_SCRIPT, MINIMAL_ENV_FILE)
        log = tmp_path / "calls-ps.log"
        result = _run_ps(repo, docker_stub_dir_ps, log, {"COMPOSE_PROJECT_NAME": REAL_PROJECT_NAME})
        assert result.returncode == 0, result.stdout + result.stderr
        _assert_pinned(_read_calls(log))


class TestTheProjectNameStaysInSyncAcrossTheRepository:
    """Five files hardcode this one name. A rename that updates only some of
    them would give the backup wrappers - or the teardown guard - a project
    that does not exist: a green backup of nothing, or a teardown guard that
    silently stops protecting the real stack, which is the exact failure this
    change and docs/incidents/0001 exist to prevent. compose-teardown.ps1 is
    included alongside compose-teardown.sh (review finding, PR #32): the
    incident doc treats the two as an equally-required pair, so a check that
    covered only the bash side would stay green while the Windows-side
    teardown guard silently drifted."""

    def test_the_compose_file_declares_it(self) -> None:
        data = cast(dict[str, Any], yaml.safe_load(DOCKER_COMPOSE_YML.read_text(encoding="utf-8")))
        assert data["name"] == REAL_PROJECT_NAME

    def test_every_wrapper_pins_the_same_name(self) -> None:
        assert re.search(
            rf'^real_project_name="{REAL_PROJECT_NAME}"$',
            BASH_SCRIPT.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        assert re.search(
            rf'^\$RealProjectName = "{REAL_PROJECT_NAME}"$',
            PS_SCRIPT.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        assert re.search(
            rf'^REAL_PROJECT_NAME="{REAL_PROJECT_NAME}"$',
            COMPOSE_TEARDOWN_SH.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        assert re.search(
            rf'^\$RealProjectName = "{REAL_PROJECT_NAME}"$',
            COMPOSE_TEARDOWN_PS1.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
