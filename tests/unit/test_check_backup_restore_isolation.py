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
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

CHECK_BACKUP_RESTORE_SH = REPO_ROOT / "scripts" / "check-backup-restore.sh"
CHECK_BACKUP_RESTORE_PS1 = REPO_ROOT / "scripts" / "check-backup-restore.ps1"
DOCKER_COMPOSE_YML = REPO_ROOT / "deploy" / "compose" / "docker-compose.yml"
EMBEDDING_CONTRACT_TEST_YML = (
    REPO_ROOT / "deploy" / "compose" / "embedding-sidecar.contract-test.docker-compose.yml"
)
BACKUP_SH = REPO_ROOT / "deploy" / "compose" / "backup" / "backup.sh"
RESTORE_TEST_SH = REPO_ROOT / "deploy" / "compose" / "backup" / "restore-test.sh"
RESTORE_TEST_YML = REPO_ROOT / "deploy" / "compose" / "backup.restore-test.docker-compose.yml"
OPERATING_MD = REPO_ROOT / "docs" / "OPERATING.md"

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


def _normalize_comment_prose(text: str) -> str:
    """Strip each line's leading `# ` marker, then collapse all remaining
    whitespace (including the newlines between wrapped lines) to single
    spaces, so a multi-line `#`-commented sentence can be matched as one
    string regardless of exactly where it wraps. A review found a narrower
    version of this (only replacing the literal substring "\\n# ") was a
    no-op against today's wrapping and would both miss a wrap that happens
    mid-word-boundary differently and leave a literal `#` in the matched text
    for a sentence that spans a line break, since plain `str.split()` alone
    does not strip comment markers."""
    stripped_lines = (line.lstrip().removeprefix("#").strip() for line in text.splitlines())
    return " ".join(line for line in stripped_lines if line)


def _strip_ps1_comments(text: str) -> str:
    """Like `_strip_shell_comments`, plus PowerShell's `<# ... #>` block
    comments - notably the file-header docstring, which is comment-heavy
    prose explaining exactly the patterns these tests forbid. A review found
    the original version of this only stripped `#` line comments, so a
    forbidden marker quoted inside the header block would have gone
    undetected as "live code" - direction of error was safe (a spurious
    failure, not a spurious pass), but worth closing properly."""
    without_blocks = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    return _strip_shell_comments(without_blocks)


class TestCheckBackupRestoreIsolation:
    def test_sh_injects_its_own_tc_backup_root(self) -> None:
        text = CHECK_BACKUP_RESTORE_SH.read_text(encoding="utf-8")
        assert "TC_BACKUP_ROOT=$backup_root_compose_value" in text, (
            "scripts/check-backup-restore.sh must write its own isolated "
            "TC_BACKUP_ROOT into the synthetic env file it hands to Compose"
        )

    def test_ps1_injects_its_own_tc_backup_root(self) -> None:
        text = CHECK_BACKUP_RESTORE_PS1.read_text(encoding="utf-8")
        assert "TC_BACKUP_ROOT=$BackupRoot" in text, (
            "scripts/check-backup-restore.ps1 must write its own isolated "
            "TC_BACKUP_ROOT into the synthetic env file it hands to Compose"
        )

    def test_sh_never_falls_back_to_the_real_backup_root(self) -> None:
        text = CHECK_BACKUP_RESTORE_SH.read_text(encoding="utf-8")
        for marker in REAL_BACKUP_ROOT_FALLBACK_MARKERS:
            assert marker not in text, (
                f"scripts/check-backup-restore.sh must never read the real "
                f"backup root as a fallback (found {marker!r}) - an unset "
                f"isolated root must be a hard error, not a silent redirect "
                f"onto the operator's real backups"
            )

    def test_ps1_never_falls_back_to_the_real_backup_root(self) -> None:
        text = CHECK_BACKUP_RESTORE_PS1.read_text(encoding="utf-8")
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
        text = CHECK_BACKUP_RESTORE_SH.read_text(encoding="utf-8")
        assert ".tc-check-backup-restore-marker" in text, (
            "cleanup must only ever remove a directory this run created - "
            "guarded on a marker file the run itself wrote, not merely a "
            "non-empty variable"
        )
        assert "rm -rf" in text

    def test_ps1_deletes_only_a_directory_it_marked_itself(self) -> None:
        text = CHECK_BACKUP_RESTORE_PS1.read_text(encoding="utf-8")
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
        text = CHECK_BACKUP_RESTORE_SH.read_text(encoding="utf-8")
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
        code_only = _strip_ps1_comments(CHECK_BACKUP_RESTORE_PS1.read_text(encoding="utf-8"))
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
        sh = CHECK_BACKUP_RESTORE_SH.read_text(encoding="utf-8")
        ps1 = CHECK_BACKUP_RESTORE_PS1.read_text(encoding="utf-8")
        for name, text in (("sh", sh), ("ps1", ps1)):
            assert "run --rm --no-deps -T --user" in text, (
                f"scripts/check-backup-restore.{name} must run its backup-root "
                f"assertions inside a container"
            )
            assert "--entrypoint bash restore-test" in text, (
                f"scripts/check-backup-restore.{name} must reach the backup "
                f"root through the restore-test service, which already mounts "
                f"it read-only and already runs as its owning uid"
            )

    def test_both_assert_the_restrictive_permission_property(self) -> None:
        for name, path in (
            ("sh", CHECK_BACKUP_RESTORE_SH),
            ("ps1", CHECK_BACKUP_RESTORE_PS1),
        ):
            text = path.read_text(encoding="utf-8")
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

    def test_the_soft_pass_is_gated_on_an_independent_enforcement_probe(self) -> None:
        """A second review round found the original version of this check
        conflated two different questions: "does this platform enforce POSIX
        bits at all" and "did backup.sh get its own chown/chmod right on the
        real backup root". Collapsing them into one `stat` comparison against
        the real root meant a genuine regression in backup.sh (a lost
        `chmod 700`) was indistinguishable from "this platform doesn't
        enforce permissions" and silently printed SKIP instead of FAIL.

        The fix answers the platform question first, with a disposable
        scratch fixture unrelated to the real backup root, and only treats a
        mismatch on the real root as a hard failure once enforcement is
        independently confirmed. These assertions pin that a scratch probe
        exists and precedes the real-root comparison - not just that "SKIP"
        and "999 700" appear somewhere in the file, which the buggy version
        also satisfied.
        """
        for name, path in (
            ("sh", CHECK_BACKUP_RESTORE_SH),
            ("ps1", CHECK_BACKUP_RESTORE_PS1),
        ):
            code_only = _strip_shell_comments(path.read_text(encoding="utf-8"))
            assert "tc-enforcement-probe" in code_only, (
                f"scripts/check-backup-restore.{name} must probe permission "
                f"enforcement with a scratch fixture independent of the real "
                f"backup root, so a mismatch on the real root can never be "
                f"mistaken for 'this platform doesn't enforce permissions'"
            )
            # Comments/prose elsewhere in the file explain the OLD, buggy
            # comparison and legitimately quote "999 700" while doing so -
            # comparing on code alone finds the actual runtime comparison,
            # not a mention of it.
            probe_index = code_only.index("tc-enforcement-probe")
            real_root_comparison_index = code_only.index('"999 700"')
            assert probe_index < real_root_comparison_index, (
                f"scripts/check-backup-restore.{name} must run the "
                f"platform-enforcement probe BEFORE comparing the real "
                f"backup root against '999 700' - answering the platform "
                f"question first is what makes a real-root mismatch a hard "
                f"failure rather than a silent SKIP"
            )

    def test_both_hand_permissions_back_before_deleting_the_backup_root(self) -> None:
        sh = CHECK_BACKUP_RESTORE_SH.read_text(encoding="utf-8")
        ps1 = CHECK_BACKUP_RESTORE_PS1.read_text(encoding="utf-8")
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
            assert "--user 0:0 --entrypoint bash backup" in text, (
                f"scripts/check-backup-restore.{name} must do the handback "
                f"through the `backup` service - it is the only one that "
                f"mounts the backup root read-write"
            )

    def test_every_docker_compose_run_against_the_backup_service_passes_its_profiles(
        self,
    ) -> None:
        """A second review round found every `docker compose run ... backup`
        call in both scripts failing outright with "no such service:
        postgres", because `backup`'s own `depends_on: postgres` gets dropped
        from Compose's resolved model entirely when no matching profile is
        active - `--no-deps` only skips STARTING a dependency, not resolving
        whether it exists at all. This had silently no-op'd the permission
        handback in cleanup on every single run (masked by `|| true`/`*>
        $null`), invisible on Windows only because Docker Desktop for Windows
        does not enforce the permissions the handback exists to undo in the
        first place. Pins that every such call carries the required flags, so
        a future call site added the same way cannot reintroduce this."""
        for name, path in (
            ("sh", CHECK_BACKUP_RESTORE_SH),
            ("ps1", CHECK_BACKUP_RESTORE_PS1),
        ):
            code_only = _strip_shell_comments(path.read_text(encoding="utf-8"))
            # Every occurrence of "--entrypoint bash backup" (this project's
            # own way of invoking a one-shot command against the `backup`
            # service) must be preceded, somewhere on the same logical
            # docker-compose invocation, by the required profile flags.
            # Checking the whole file for at least one occurrence of the
            # paired pattern is enough here: both current call sites (the
            # cleanup handback and, on the .sh, backup_root_write_exec) are
            # the only two ways this project invokes the `backup` service
            # this way, and both must carry the flags identically.
            entrypoint_count = code_only.count("--entrypoint bash backup")
            assert entrypoint_count >= 1, (
                f"scripts/check-backup-restore.{name} should invoke the "
                f"`backup` service via --entrypoint bash at least once"
            )
            profile_flagged_count = code_only.count("--profile core --profile backup")
            assert profile_flagged_count >= entrypoint_count, (
                f"scripts/check-backup-restore.{name} has {entrypoint_count} "
                f"call(s) targeting the `backup` service via --entrypoint but "
                f"only {profile_flagged_count} carrying '--profile core "
                f"--profile backup' - every `docker compose run` against a "
                f"profile-gated service needs its profiles even with "
                f"--no-deps, or it fails with 'no such service: postgres'"
            )


class TestAttachmentCopyHappensAfterTheSnapshot:
    """Finding F3. backup.sh used to copy attachments in its root phase, before
    the privilege drop and therefore before the coprocess exported the MVCC
    snapshot pg_dump reads through - so a blob written and committed in that
    window was in the dump but not in the backup directory. The full behaviour
    is covered by scripts/check-backup-restore.sh's round 2, which needs Docker;
    these are the cheap static guards that stop a refactor from quietly
    restoring the original ordering.

    Note on what CANNOT be asserted here: the fix forks the privilege drop
    instead of `exec`-ing it, so root's copy and the child's snapshot export
    live in the same file and root's copy is lexically EARLIER than the
    `pg_export_snapshot()` call it now waits for. Line order therefore says
    nothing about run order; the handshake is what orders them, so the
    handshake is what these tests pin.
    """

    def test_the_privilege_drop_is_a_fork_not_a_one_way_exec(self) -> None:
        text = BACKUP_SH.read_text(encoding="utf-8")
        code_only = _strip_shell_comments(text)
        assert "exec gosu" not in code_only, (
            "backup.sh must not `exec gosu` into the database phase: root has "
            "to stay alive to perform the attachment copy AFTER the child has "
            "exported its snapshot"
        )
        assert 'gosu postgres /bin/bash "$0" --db-phase "$@" &' in code_only, (
            "backup.sh's root phase must fork the database phase (and pass "
            "--db-phase so the child does not re-enter the root branch)"
        )

    def test_root_waits_for_the_snapshot_signal_before_copying(self) -> None:
        code_only = _strip_shell_comments(BACKUP_SH.read_text(encoding="utf-8"))
        wait_index = code_only.index("read -r -t 5 -u 3 snapshot_signal")
        copy_index = code_only.index("cp -au --parents -t")
        assert wait_index < copy_index, (
            "backup.sh's root phase must block on the 'snapshot exported' "
            "signal before it starts copying attachments - a copy taken before "
            "the snapshot instant can miss a blob the dump references"
        )

    def test_the_child_signals_only_after_exporting_the_snapshot(self) -> None:
        code_only = _strip_shell_comments(BACKUP_SH.read_text(encoding="utf-8"))
        export_index = code_only.index("SELECT pg_export_snapshot();")
        # The specific write, not the bare redirection: root's own
        # `exec 3<>"$snapshot_ready_fifo"` also contains that substring and
        # sits lexically before the export.
        signal_index = code_only.index("""printf 'snapshot-ready""")
        assert export_index < signal_index, (
            "backup.sh's database phase must export its snapshot before "
            "releasing root's copy - the signal is what makes the copy a "
            "superset of what the dump references"
        )

    def test_the_manifest_waits_for_the_copy_to_finish(self) -> None:
        code_only = _strip_shell_comments(BACKUP_SH.read_text(encoding="utf-8"))
        wait_index = code_only.index(
            'read -r -t "$copy_complete_timeout_seconds" -u 5 _copy_signal'
        )
        manifest_index = code_only.index("find . -type f -print0 | sort -z | xargs -0 sha256sum")
        assert wait_index < manifest_index, (
            "backup.sh's database phase must block on 'copy complete' before "
            "hashing the copied tree, or the manifest describes a half-finished "
            "copy"
        )

    def test_in_flight_blob_temp_files_are_excluded_from_the_copy(self) -> None:
        code_only = _strip_shell_comments(BACKUP_SH.read_text(encoding="utf-8"))
        assert "! -name '.incoming-*'" in code_only, (
            "backup.sh must exclude BlobStore.put's in-flight temp files "
            "(mkstemp prefix '.incoming-') from the copy: they are never "
            "referenced by a `blobs` row, and copying one mid-write puts a "
            "torn file into the manifest"
        )

    def test_round_2_writes_real_blobs_in_blob_store_order(self) -> None:
        for name, path in (
            ("sh", CHECK_BACKUP_RESTORE_SH),
            ("ps1", CHECK_BACKUP_RESTORE_PS1),
        ):
            text = path.read_text(encoding="utf-8")
            assert "INSERT INTO blobs" in text, (
                f"scripts/check-backup-restore.{name}'s round 2 must commit "
                f"real `blobs` rows concurrently with the backup - a writer "
                f"that only inserts into `thoughts` never exercises the "
                f"copy-ordering finding at all"
            )
            assert "/data/attachments/$key" in text, (
                f"scripts/check-backup-restore.{name}'s round 2 must write the "
                f"blob file into the attachments volume, at the fan-out path "
                f"blob_store.storage_key_for produces, before committing its row"
            )
            assert "COMMITTED " in text, (
                f"scripts/check-backup-restore.{name} must count the blob "
                f"writer's committed blobs and fail the round if it never "
                f"committed one - otherwise a writer failing on every iteration "
                f"makes this round pass while testing nothing"
            )


class TestEmbeddingSidecarNetworkIsolation:
    def test_base_compose_file_declares_an_internal_embedding_network(self) -> None:
        text = DOCKER_COMPOSE_YML.read_text(encoding="utf-8")
        assert "embedding:\n    internal: true" in text, (
            "deploy/compose/docker-compose.yml must declare a top-level "
            "'embedding' network with internal: true"
        )

    def test_embedding_sidecar_is_attached_only_to_the_embedding_network(self) -> None:
        text = DOCKER_COMPOSE_YML.read_text(encoding="utf-8")
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
        text = EMBEDDING_CONTRACT_TEST_YML.read_text(encoding="utf-8")
        assert "networks: [embedding, embedding-contract-test]" in text, (
            "the contract-test overlay must attach embedding-sidecar to a "
            "second, non-internal network for its own published port to "
            "work - an internal: true-only network silently drops a "
            "container's published host port (verified on Docker 29.2.1)"
        )
        assert "embedding-contract-test:\n    driver: bridge" in text


class TestRestoreTestNetworkIsolation:
    """Finding F5. The restore-validation stack declared no `networks:` at all,
    so both services landed on Compose's implicit `default` bridge, which has a
    gateway and full outbound NAT - while that job restores a dump whose only
    integrity evidence is a checksum stored in the same mutable directory as
    the dump, and then runs `pg_restore` over it. Parsed rather than
    grepped: a service that silently loses its `networks:` key rejoins
    `default` and undoes the fix without changing any string these tests would
    otherwise match.
    """

    def _compose(self) -> dict[str, Any]:
        return cast("dict[str, Any]", yaml.safe_load(RESTORE_TEST_YML.read_text(encoding="utf-8")))

    def test_the_declared_network_is_internal(self) -> None:
        networks = self._compose()["networks"]
        assert list(networks) == ["restore-test"], (
            "deploy/compose/backup.restore-test.docker-compose.yml must declare "
            "exactly one network, so a service cannot join a second, "
            "egress-capable one by accident"
        )
        assert networks["restore-test"]["internal"] is True, (
            "the restore-test network must be internal: true - nothing in this "
            "stack has any reason to reach the internet, and it handles a dump "
            "whose provenance nothing here can verify"
        )

    def test_every_service_joins_that_network_and_only_that_network(self) -> None:
        services = self._compose()["services"]
        assert set(services) == {"postgres-scratch", "restore-test"}
        for name, service in services.items():
            assert service.get("networks") == ["restore-test"], (
                f"{name} must declare `networks: [restore-test]` - a service "
                f"without a networks: key silently rejoins the egress-capable "
                f"`default` network and undoes the isolation"
            )

    def test_restore_test_states_that_its_integrity_evidence_is_self_declared(
        self,
    ) -> None:
        text = RESTORE_TEST_SH.read_text(encoding="utf-8")
        normalized = _normalize_comment_prose(text)
        assert "does not defend against a tampered backup" in normalized, (
            "deploy/compose/backup/restore-test.sh's header must say plainly "
            "that its checksums live beside the dump they describe, so a green "
            "run is not provenance - signed backups are deferred behind "
            "docs/DESIGN.md 19's key-custody decision, and an operator must not "
            "read the deferral as 'already handled'"
        )
        assert "does NOT sandbox local execution" in normalized, (
            "deploy/compose/backup/restore-test.sh's header must also say "
            "that network isolation is not an execution sandbox - pg_restore "
            "runs a tampered dump's SQL as the scratch cluster's own "
            "superuser, so an operator must not read 'internal: true' as "
            "'safe to restore anything'"
        )

    def test_operating_md_states_the_same_caveat_operators_actually_read(
        self,
    ) -> None:
        """A review found the threat note lived only in restore-test.sh's
        header, which operators are unlikely to open - the network fix could
        regress its documentation in docs/OPERATING.md (what an operator
        actually reads before restoring) while this suite stayed green,
        because nothing guarded that surface. This guards it too."""
        normalized = " ".join(OPERATING_MD.read_text(encoding="utf-8").split())
        assert "is not provenance" in normalized, (
            "docs/OPERATING.md must state, next to the restore instructions, "
            "that a green restore-test is not provenance"
        )
        assert "does NOT sandbox local execution" in normalized, (
            "docs/OPERATING.md must also carry the local-execution caveat, "
            "not only the network-egress one - an operator who reads only "
            "'internal: true network' could otherwise conclude restoring an "
            "untrusted dump is safe outright"
        )
