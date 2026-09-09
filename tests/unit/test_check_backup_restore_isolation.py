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
