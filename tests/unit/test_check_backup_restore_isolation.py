"""Static regression guards for two PR #32 review findings that a full
Docker-driven run of scripts/check-backup-restore.sh/.ps1 or scripts/check.sh/
.ps1 cannot cheaply exercise on every test run.

Finding 1 (routine checks overwrote the real backup pointer): a synthetic
`check-backup-restore` run that leaves TC_BACKUP_ROOT unset lets Compose fall
back to the operator's ambient environment or the real default
(deploy/compose/backups), so a routine `check.sh` run overwrote the operator's
actual `latest.txt`. These tests assert, by static inspection, that both
scripts (a) inject their own TC_BACKUP_ROOT into the synthetic env file and
(b) never contain the old real-root-fallback string - the same style of cheap
regression guard tests/unit/test_compose_teardown_guard.py already uses for
the teardown wrapper.

Finding F1 (the backup root is unreadable to the invoking user on Linux):
backup.sh leaves its output owned by uid 999, mode 0700. A Linux bind mount
enforces that; a Docker Desktop for Windows bind mount silently ignores it. Any
host-side file assertion against the backup root is therefore
platform-divergent by construction - it passed on Windows and failed in CI with
a misleading "backup did not copy the seeded attachment". These tests assert
that neither script has a host-side file assertion against the backup root any
more, that both read it through a container instead, that both assert the
restrictive-permission property on purpose, and that both hand permissions back
through a container before deleting the tree.

Finding 4 (the embedding sidecar was not network-isolated): asserts, by
static inspection of the compose YAML, that the base file attaches
embedding-sidecar to ONLY the internal `embedding` network, and that the
contract-test overlay adds a second, non-internal network so its published
port keeps working under that overlay.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

CHECK_BACKUP_RESTORE_SH = REPO_ROOT / "scripts" / "check-backup-restore.sh"
CHECK_BACKUP_RESTORE_PS1 = REPO_ROOT / "scripts" / "check-backup-restore.ps1"
DOCKER_COMPOSE_YML = REPO_ROOT / "deploy" / "compose" / "docker-compose.yml"
EMBEDDING_CONTRACT_TEST_YML = (
    REPO_ROOT / "deploy" / "compose" / "embedding-sidecar.contract-test.docker-compose.yml"
)

# The exact fallback the review found: reading TC_BACKUP_ROOT with a default
# of the real backup directory silently redirects onto the operator's real
# backups whenever the synthetic env file doesn't set it.
REAL_BACKUP_ROOT_FALLBACK_MARKERS = (
    "${TC_BACKUP_ROOT:-deploy/compose/backups}",
    '"deploy/compose/backups"',
)


def _strip_shell_comments(text: str) -> str:
    """Drop whole-line `#` comments so a marker quoted inside this script's own
    explanatory prose (there is a lot of it, and it names the exact patterns
    these tests forbid) is not mistaken for live code."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _strip_ps1_comments(text: str) -> str:
    return _strip_shell_comments(text)


class TestCheckBackupRestoreIsolation:
    def test_sh_injects_its_own_tc_backup_root(self) -> None:
        text = CHECK_BACKUP_RESTORE_SH.read_text()
        assert "TC_BACKUP_ROOT=$backup_root_compose_value" in text, (
            "scripts/check-backup-restore.sh must write its own isolated "
            "TC_BACKUP_ROOT into the synthetic env file it hands to Compose"
        )

    def test_ps1_injects_its_own_tc_backup_root(self) -> None:
        text = CHECK_BACKUP_RESTORE_PS1.read_text()
        assert "TC_BACKUP_ROOT=$BackupRoot" in text, (
            "scripts/check-backup-restore.ps1 must write its own isolated "
            "TC_BACKUP_ROOT into the synthetic env file it hands to Compose"
        )

    def test_sh_never_falls_back_to_the_real_backup_root(self) -> None:
        text = CHECK_BACKUP_RESTORE_SH.read_text()
        for marker in REAL_BACKUP_ROOT_FALLBACK_MARKERS:
            assert marker not in text, (
                f"scripts/check-backup-restore.sh must never read the real "
                f"backup root as a fallback (found {marker!r}) - an unset "
                f"isolated root must be a hard error, not a silent redirect "
                f"onto the operator's real backups"
            )

    def test_ps1_never_falls_back_to_the_real_backup_root(self) -> None:
        text = CHECK_BACKUP_RESTORE_PS1.read_text()
        # The exact fallback the review found, ported to PowerShell: reading
        # $env:TC_BACKUP_ROOT with a default of the real backup directory. A
        # bare substring check on "deploy/compose/backups" would also trip
        # on this file's own explanatory comments about the real default,
        # so this strips comments first and matches only code.
        code_only = re.sub(r"#.*", "", text)
        assert '"deploy/compose/backups"' not in code_only, (
            "scripts/check-backup-restore.ps1 must never read the real "
            "backup root as a fallback value in code - an unset isolated "
            "root must be a hard error, not a silent redirect onto the "
            "operator's real backups"
        )

    def test_sh_deletes_only_a_directory_it_marked_itself(self) -> None:
        text = CHECK_BACKUP_RESTORE_SH.read_text()
        assert ".tc-check-backup-restore-marker" in text, (
            "cleanup must only ever remove a directory this run created - "
            "guarded on a marker file the run itself wrote, not merely a "
            "non-empty variable"
        )
        assert "rm -rf" in text

    def test_ps1_deletes_only_a_directory_it_marked_itself(self) -> None:
        text = CHECK_BACKUP_RESTORE_PS1.read_text()
        assert ".tc-check-backup-restore-marker" in text, (
            "cleanup must only ever remove a directory this run created - "
            "guarded on a marker file the run itself wrote, not merely a "
            "non-empty variable"
        )
        assert "Remove-Item -LiteralPath $BackupRoot -Recurse" in text


class TestBackupRootIsReadThroughAContainer:
    """Finding F1. The property under test in the harness ("a real attachment
    is copied and hashed") must survive; the mechanism that reads it must stop
    assuming the invoking user can read root/postgres-owned 0700 paths."""

    def test_sh_has_no_host_side_file_assertion_on_the_backup_root(self) -> None:
        text = CHECK_BACKUP_RESTORE_SH.read_text()
        code_only = _strip_shell_comments(text)
        for marker in ('[[ -f "$backup_root_host', '[[ ! -f "$backup_root_host'):
            assert marker not in code_only, (
                f"scripts/check-backup-restore.sh must not test files under the "
                f"backup root from the host (found {marker!r}) - on Linux that "
                f"tree is 0700 and owned by uid 999, so the invoking user gets "
                f"EACCES and the script reports a copy failure that never "
                f"happened. Read it through a container instead."
            )
        assert 'cat "$backup_root_host' not in code_only
        assert 'grep -q "^${attachment_sha256}' not in code_only

    def test_ps1_has_no_host_side_file_assertion_on_the_backup_root(self) -> None:
        code_only = _strip_ps1_comments(CHECK_BACKUP_RESTORE_PS1.read_text())
        for marker in (
            "Test-Path -LiteralPath $CopiedAttachment",
            'Join-Path $BackupRoot "attachments/ab/synthetic.txt"',
            'Join-Path $BackupRoot "latest.txt"',
        ):
            assert marker not in code_only, (
                f"scripts/check-backup-restore.ps1 must not read files under "
                f"the backup root from the host (found {marker!r}) - it must "
                f"stay in lockstep with the .sh, which cannot do so on Linux"
            )

    def test_both_read_the_backup_root_through_the_restore_test_service(self) -> None:
        sh = CHECK_BACKUP_RESTORE_SH.read_text()
        ps1 = CHECK_BACKUP_RESTORE_PS1.read_text()
        for name, text in (("sh", sh), ("ps1", ps1)):
            assert "run --rm --no-deps -T --user" in text, (
                f"scripts/check-backup-restore.{name} must run its backup-root "
                f"assertions inside a container"
            )
            assert "--entrypoint /bin/bash restore-test" in text, (
                f"scripts/check-backup-restore.{name} must reach the backup "
                f"root through the restore-test service, which already mounts "
                f"it read-only and already runs as its owning uid"
            )

    def test_both_assert_the_restrictive_permission_property(self) -> None:
        for name, path in (
            ("sh", CHECK_BACKUP_RESTORE_SH),
            ("ps1", CHECK_BACKUP_RESTORE_PS1),
        ):
            text = path.read_text()
            assert "999 700" in text, (
                f"scripts/check-backup-restore.{name} must assert that the "
                f"backup root really is uid-999/0700 - the permission bits are "
                f"a security property of backup.sh (full plaintext database "
                f"and attachments), not an accident to work around"
            )
            assert "NOT-READABLE" in text, (
                f"scripts/check-backup-restore.{name} must probe the backup "
                f"root as an unrelated non-root uid and require that it cannot "
                f"read even latest.txt"
            )
            assert "SKIP:" in text, (
                f"scripts/check-backup-restore.{name} must soft-pass the "
                f"permission property, loudly, where the bind mount does not "
                f"enforce POSIX bits - silently passing there would look "
                f"identical to the property actually holding"
            )

    def test_both_hand_permissions_back_before_deleting_the_backup_root(self) -> None:
        sh = CHECK_BACKUP_RESTORE_SH.read_text()
        ps1 = CHECK_BACKUP_RESTORE_PS1.read_text()
        assert "chown -R $(id -u):$(id -g) /backups" in sh, (
            "scripts/check-backup-restore.sh's cleanup must hand the backup "
            "root back to the invoking uid from inside a container - the host "
            "cannot rm -rf a 0700 tree owned by uid 999, so the directory is "
            "left behind and the failing rm can mask the real exit code"
        )
        assert "chmod -R a+rwX /backups" in ps1, (
            "scripts/check-backup-restore.ps1's cleanup must relax the backup "
            "root from inside a container for the same reason as the .sh"
        )
        for name, text in (("sh", sh), ("ps1", ps1)):
            assert "--user 0:0 --entrypoint /bin/bash backup" in text, (
                f"scripts/check-backup-restore.{name} must do the handback "
                f"through the `backup` service - it is the only one that "
                f"mounts the backup root read-write"
            )


class TestEmbeddingSidecarNetworkIsolation:
    def test_base_compose_file_declares_an_internal_embedding_network(self) -> None:
        text = DOCKER_COMPOSE_YML.read_text()
        assert "embedding:\n    internal: true" in text, (
            "deploy/compose/docker-compose.yml must declare a top-level "
            "'embedding' network with internal: true"
        )

    def test_embedding_sidecar_is_attached_only_to_the_embedding_network(self) -> None:
        text = DOCKER_COMPOSE_YML.read_text()
        start = text.index("  embedding-sidecar:")
        # The next top-level (exactly 2-space-indented, non-blank) service
        # key ends this service's own block. A plain "\n  " substring search
        # is not enough - it also matches the first two spaces of every
        # 4-space-indented line INSIDE this service's own block, ending the
        # extraction after the first nested key.
        match = re.search(r"\n {2}\S", text[start + len("  embedding-sidecar:\n") :])
        assert match, "could not find the end of the embedding-sidecar service block"
        end = start + len("  embedding-sidecar:\n") + match.start()
        service_block = text[start:end]
        assert "networks: [embedding]" in service_block, (
            "embedding-sidecar must be attached to only the 'embedding' "
            "network in the base compose file - it has no authentication "
            "of its own, so it must never land on the egress-capable "
            "default network"
        )

    def test_contract_test_overlay_adds_a_second_non_internal_network(self) -> None:
        text = EMBEDDING_CONTRACT_TEST_YML.read_text()
        assert "networks: [embedding, embedding-contract-test]" in text, (
            "the contract-test overlay must attach embedding-sidecar to a "
            "second, non-internal network for its own published port to "
            "work - an internal: true-only network silently drops a "
            "container's published host port (verified on Docker 29.2.1)"
        )
        assert "embedding-contract-test:\n    driver: bridge" in text
