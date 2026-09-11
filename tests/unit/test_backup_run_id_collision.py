"""Regression coverage for a P1 review finding: deploy/compose/backup/backup.sh
named every artifact - the dump, row-counts/grants JSON, attachments
manifest, and the backup manifest itself - using only a second-resolution
`$timestamp`. The backup lock (`flock`) guarantees two runs never WRITE at the
same time, but says nothing about two serialized runs starting in the same
second: those computed the identical `$timestamp` and so collided on every
artifact filename. If the second (colliding) run then failed after
overwriting the first run's identically-named dump/manifest files but before
reaching its own `latest.txt` write, `latest.txt` was left pointing at a
manifest describing the FIRST run's dump - by sha256 and row counts - while
the file actually on disk under that name was now the second, failed run's.
The last known-good backup was silently invalidated by a run that never
itself succeeded.

The fix replaces the bare timestamp with a `$run_id` (the same timestamp,
plus a random `mktemp -u` suffix) in every artifact filename, keeping the
timestamp only as an informational JSON field.

This module proves two independent things, matching how the rest of this
file group is tested (see test_check_backup_restore_isolation.py's own
docstring): the script only ever runs as root inside a container, so a true
end-to-end "two real runs collide" integration test is not cheaply available
here. What IS cheaply testable without Docker/root/postgres:

1. The run-id-GENERATING snippet, extracted verbatim from the real script (not
   reimplemented) and executed twice under a frozen `date`, actually produces
   two different values - proving same-second collision is closed, not merely
   asserting the code "looks" collision-resistant.
2. Static, textual checks that every artifact filename is built from
   `$run_id` and not the bare `$timestamp`, and that `latest.txt` is written
   only as the LAST statement in the script - so a run that fails at any
   earlier step (including a run that collided with nothing, thanks to #1)
   can never have touched `latest.txt`, and thanks to #1 can never have
   touched a prior run's identically-named files either.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP_SH = REPO_ROOT / "deploy" / "compose" / "backup" / "backup.sh"

# The frozen "clock" value the stubbed `date` always returns, regardless of
# real wall-clock time - simulating two runs that land in the same second.
FROZEN_TIMESTAMP = "20260101T000000Z"

DATE_STUB = f"""#!/usr/bin/env bash
# Only backup.sh's own invocation shape is stubbed; anything else falls
# through to the real `date` so this stub cannot mask an unrelated call.
if [[ "$*" == "-u +%Y%m%dT%H%M%SZ" ]]; then
  printf '%s\\n' "{FROZEN_TIMESTAMP}"
else
  exec /usr/bin/env date "$@"
fi
"""


def _strip_shell_comments(text: str) -> str:
    """Drop whole-line `#` comments, matching the helper of the same name in
    test_check_backup_restore_isolation.py - duplicated locally rather than
    imported, following this test suite's existing convention of each file
    owning its own small path/helper constants (see test_backup_project_pinning.py)."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _extract_run_id_snippet(code: str) -> str:
    """Pull the run-id-generating lines verbatim out of the real backup.sh,
    from its `timestamp=` assignment through its `run_id=` assignment
    (inclusive), so the test exercises the ACTUAL code rather than a
    hand-copied reimplementation that could silently drift from it."""
    match = re.search(
        r'^timestamp="\$\(date -u \+%Y%m%dT%H%M%SZ\)"$.*?^run_id="\$\{timestamp\}-\$\{run_suffix##\*/\}"$',
        code,
        re.MULTILINE | re.DOTALL,
    )
    assert match, (
        "could not find the timestamp=...run_id=... block in "
        "deploy/compose/backup/backup.sh by its expected anchor lines - "
        "either it was renamed/restructured (update this test's regex to "
        "match) or the collision-proofing fix was reverted"
    )
    return match.group(0)


@pytest.fixture
def date_stub_dir(tmp_path: Path) -> Path:
    stub_dir = tmp_path / "date-stub-bin"
    stub_dir.mkdir()
    date_stub = stub_dir / "date"
    date_stub.write_text(DATE_STUB, newline="\n")
    date_stub.chmod(0o755)
    return stub_dir


@pytest.mark.skipif(
    sys.platform == "win32" and not shutil.which("bash"),
    reason="requires a bash interpreter (Git Bash on Windows) to execute the extracted snippet",
)
class TestRunIdIsCollisionResistantUnderAFrozenClock:
    def _run_snippet_once(self, snippet: str, date_stub_dir: Path, tmp_path: Path) -> str:
        bash = shutil.which("bash")
        assert bash, "bash is required for this test"
        script = tmp_path / f"run_id_probe_{os.urandom(4).hex()}.sh"
        script.write_text(
            f"#!/usr/bin/env bash\nset -euo pipefail\n{snippet}\nprintf '%s' \"$run_id\"\n"
        )
        script.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{date_stub_dir}{os.pathsep}{env.get('PATH', '')}"
        result = subprocess.run(
            [bash, str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"run-id probe script failed: {result.stderr}\nscript:\n{snippet}"
        )
        return result.stdout

    def test_two_runs_under_a_frozen_clock_produce_different_run_ids(
        self, date_stub_dir: Path, tmp_path: Path
    ) -> None:
        code = BACKUP_SH.read_text(encoding="utf-8")
        snippet = _extract_run_id_snippet(code)

        first = self._run_snippet_once(snippet, date_stub_dir, tmp_path)
        second = self._run_snippet_once(snippet, date_stub_dir, tmp_path)

        assert first.startswith(f"{FROZEN_TIMESTAMP}-"), (
            f"expected the frozen-clock timestamp prefix, got: {first!r} - "
            "the date stub may not have taken effect, which would make this "
            "test's 'same second' premise untested"
        )
        assert second.startswith(f"{FROZEN_TIMESTAMP}-"), (
            f"expected the frozen-clock timestamp prefix, got: {second!r}"
        )
        assert first != second, (
            "two runs computed under the IDENTICAL frozen timestamp produced "
            "the same run_id - the collision-proofing fix is not actually "
            "collision-proof: same-second serialized runs would overwrite "
            f"each other's artifacts again (both produced {first!r})"
        )


class TestArtifactFilenamesUseTheRunIdNotTheBareTimestamp:
    """`$timestamp` alone is not collision-proof (see module docstring). Every
    path a run actually creates on disk must be namespaced by `$run_id`
    instead - `$timestamp` may still appear as an informational JSON field,
    just never inside a filename."""

    def _code(self) -> str:
        return _strip_shell_comments(BACKUP_SH.read_text(encoding="utf-8"))

    @pytest.mark.parametrize(
        "assignment",
        [
            'dump_tmp="$postgres_dir/.tmp-$run_id.dump"',
            'dump_final="$postgres_dir/thought_capture-$run_id.dump"',
            'row_counts_json="$postgres_dir/thought_capture-$run_id.row-counts.json"',
            'grants_json="$postgres_dir/thought_capture-$run_id.grants.json"',
            'manifest_file="$attachments_dir/../attachments-$run_id.sha256"',
            'manifest_json="$backup_root/thought_capture-$run_id.manifest.json"',
        ],
    )
    def test_each_artifact_path_is_built_from_run_id(self, assignment: str) -> None:
        assert assignment in self._code(), (
            f"expected this exact run_id-based path assignment, not found: {assignment!r} - "
            "a filename may have reverted to the bare, collision-prone $timestamp"
        )

    def test_no_artifact_path_assignment_still_uses_the_bare_timestamp(self) -> None:
        code = self._code()
        for line in code.splitlines():
            stripped = line.strip()
            if not re.match(
                r"^(dump_tmp|dump_final|row_counts_json|grants_json|manifest_file|manifest_json)=",
                stripped,
            ):
                continue
            assert "$run_id" in stripped and "$timestamp" not in stripped, (
                f"artifact path assignment does not use the collision-proof $run_id: {stripped!r}"
            )

    def test_the_manifest_json_keeps_timestamp_as_informational_and_adds_run_id(self) -> None:
        code = self._code()
        assert '"timestamp": "$timestamp",' in code, (
            "the manifest JSON should keep a human-readable timestamp field "
            "even though it no longer guarantees filename uniqueness"
        )
        assert '"run_id": "$run_id",' in code, (
            "the manifest JSON must record the actual run_id used for its "
            "own and its sibling artifacts' filenames, for traceability"
        )


class TestLatestTxtIsWrittenOnlyAfterEveryRunScopedArtifact:
    """A run that fails at any point before its own final `latest.txt` write
    cannot have touched `latest.txt` at all - and, given
    TestRunIdIsCollisionResistantUnderAFrozenClock above, cannot have
    collided with any other run's filenames either. Together these two
    classes are the proof that a failed second run cannot invalidate the
    last known-good backup: it cannot silently overwrite a prior run's
    dump/manifest (no filename collision), and it cannot repoint
    `latest.txt` at its own, possibly-incomplete work unless it reaches the
    very last line of the script."""

    def _code(self) -> str:
        return _strip_shell_comments(BACKUP_SH.read_text(encoding="utf-8"))

    def test_latest_txt_write_is_the_last_of_every_run_scoped_artifact_write(self) -> None:
        code = self._code()
        latest_txt_index = code.index('mv "$latest_tmp" "$backup_root/latest.txt"')
        for artifact_write in (
            'mv "$dump_tmp" "$dump_final"',
            'printf \'%s\\n\' "$row_counts_json_text" >"$row_counts_json"',
            'printf \'%s\\n\' "$grants_json_text" >"$grants_json"',
            'chmod 600 "$manifest_file"',
            'chmod 600 "$manifest_json"',
        ):
            assert code.index(artifact_write) < latest_txt_index, (
                f"expected {artifact_write!r} to complete before latest.txt is "
                "written - latest.txt must be the last thing a run does, so a "
                "run that fails earlier never repoints it"
            )
