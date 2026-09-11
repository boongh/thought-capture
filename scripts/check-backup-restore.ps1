<#
.SYNOPSIS
Proves scripts/backup.ps1's dump can actually be restored, as part of
scripts/check.ps1 - not merely that pg_dump exits 0.

.DESCRIPTION
Deliberately does NOT reuse scripts/backup.ps1/restore-test.ps1 directly:
those intentionally target the real `thought-capture` project (no -p) so a
real invocation backs up real data. A routine check.ps1 run must never
start, reuse, or touch the developer's real dev stack, and must never
collide with the port check.ps1's own "integration tests" step may already
be using - so this brings up its own throwaway, uniquely-named,
uniquely-ported project instead, with a synthetic env (same pattern
check.ps1's own "compose config sanity" step already uses), and tears it
down unconditionally on exit.

TC_BACKUP_ROOT isolation (review finding): this check used to leave
TC_BACKUP_ROOT unset in its synthetic env file, which meant Compose
resolved the `backup`/`restore-test` services' bind mount from the
operator's ambient environment or the real default
(deploy/compose/backups) - so a routine check.ps1 run overwrote the
operator's actual `latest.txt` with a synthetic backup, and its negative
test round deliberately left that pointer referencing a corrupt one. This
script now creates its OWN per-run temp directory, injects it into both the
container side (via the synthetic env file) and the host side (this script
reads its own variable, never a `$env:TC_BACKUP_ROOT`-or-default fallback
onto the real root), and deletes it on exit.

PLATFORM TRAP - POSIX permission bits (review finding F1). backup.sh
deliberately finishes by chowning its output to `postgres` (uid 999) and
chmod 700-ing the backup root. A Linux bind mount enforces those bits for
real, so the INVOKING user cannot stat, read, or delete anything inside that
tree; a Docker Desktop for Windows bind mount silently ignores the same
chown/chmod, so an identical host-side check passes here and fails in CI.
That divergence is why this pair's original host-side assertions were green
on Windows and red on Linux with a misleading "backup did not copy the seeded
attachment" - the file WAS copied; the runner simply could not see it.
Consequently: any host-side file assertion against the backup root is
platform-divergent by construction. Read the backup root through a CONTAINER
instead (the `restore-test` service already bind-mounts it read-only, already
runs as the uid that owns it, and Compose already resolves the path correctly
on both platforms), and relax permissions through a container before deleting
it. scripts/check-backup-restore.sh does the same thing, the same way.
#>


# Deliberately NOT $ErrorActionPreference = "Stop": with it set, Windows
# PowerShell 5.1 turns any line `docker compose` writes to stderr into a
# terminating error - even on exit code 0. Confirmed directly while testing
# scripts/compose-teardown.ps1: a real, successful `docker compose` call
# that only warned still crashed the script. Real failures are still caught
# below via explicit $LASTEXITCODE checks.

Set-Location (Join-Path $PSScriptRoot "..") -ErrorAction Stop

$BackupProject = "thought-capture-check-backup"
# A name distinct from every real dev-stack container (CLAUDE.md's Docker
# section): round 2 runs this as a long-lived writer against the throwaway
# project's attachments volume and database.
$BlobWriterContainer = "tc-check-backup-blob-writer"
$RestoreProject = "thought-capture-check-restore-test"
$ComposeFile = "deploy/compose/docker-compose.yml"
$RestoreComposeFile = "deploy/compose/backup.restore-test.docker-compose.yml"

# Per-run, isolated backup root - an absolute Windows path (system temp,
# via .NET's GetTempPath) with backslashes converted to forward slashes so
# Compose accepts it as a bind-mount source. This is safe here in a way
# scripts/check-backup-restore.sh's own `mktemp -d` is NOT: confirmed
# directly in this session against a live Docker Desktop instance - a
# native Windows absolute path (e.g. "C:/Users/.../AppData/Local/Temp/...")
# written into a synthetic env file and read by `docker compose` (invoked
# natively, not via Git Bash) bind-mounts correctly, while Git Bash's own
# `mktemp -d` produces an MSYS-style "/tmp/..." path that Git Bash itself
# resolves correctly for ITS OWN file checks but that Docker Desktop's
# WSL2 backend silently resolves as a path INSIDE its own Linux VM when it
# is merely file content (not a Git-Bash-translated argv) - the reason
# check-backup-restore.sh creates its isolated root under deploy/compose/
# instead. This script is unaffected because it never goes through Git
# Bash's own path translation layer at all.
#
# A marker file inside it is what cleanup below checks before ever calling
# Remove-Item -Recurse on it, so a future edit that changes how this
# variable is computed can never silently turn cleanup into a recursive
# delete of an operator path.
$BackupRoot = Join-Path ([System.IO.Path]::GetTempPath()) "tc-check-backup-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $BackupRoot -ErrorAction Stop | Out-Null
$BackupRoot = $BackupRoot -replace '\\', '/'
$BackupRootMarker = Join-Path $BackupRoot ".tc-check-backup-restore-marker"
New-Item -ItemType File -Path $BackupRootMarker -ErrorAction Stop | Out-Null

# Positive guard (review finding, step 4): assert the isolated root can
# never collide with the real default backup root (deploy/compose/backups,
# resolved relative to $ComposeFile's directory - the project directory
# Compose uses for a relative TC_BACKUP_ROOT-or-default bind-mount source).
# A freshly generated GUID under the system temp directory should never
# produce this path, but the check exists so a change to either side fails
# loudly instead of silently checking against the real root again.
$RealBackupRootDefault = (Join-Path (Resolve-Path (Split-Path $ComposeFile)) "backups") -replace '\\', '/'
if ($BackupRoot -eq $RealBackupRootDefault) {
    throw "the isolated backup root resolved to the real default backup root ($RealBackupRootDefault) - refusing to run"
}

# Container-side assertion scripts live in their own throwaway directory,
# bind-mounted read-only into the assertion containers below. Deliberately
# NOT passed as `bash -c "<script text>"` arguments the way
# scripts/check-backup-restore.sh does: Windows PowerShell 5.1's native-command
# argument serialization mishandles a quoted string that itself contains
# quotes and spaces (the same hazard already documented for the attachment
# seed file further down), and these scripts are full of both. A bind-mounted
# file never goes through argv encoding at all.
$AssertScriptDir = Join-Path ([System.IO.Path]::GetTempPath()) "tc-check-assert-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $AssertScriptDir -ErrorAction Stop | Out-Null
$AssertScriptDir = $AssertScriptDir -replace '\\', '/'

function Write-AssertScript {
    param([string]$Name, [string]$Body)
    # LF, no BOM: this file is executed by bash inside a Linux container, where
    # CRLF produces an obscure "\r: command not found" and a BOM breaks the
    # first line outright.
    [System.IO.File]::WriteAllText(
        "$AssertScriptDir/$Name",
        ($Body -replace "`r`n", "`n"),
        [System.Text.UTF8Encoding]::new($false)
    )
}

# Set to $true once the first `backup` run has written into the isolated
# backup root; Invoke-Cleanup reads it to decide whether the permission
# handback below is needed at all.
$BackupHasRun = $false

$EnvFile = New-TemporaryFile -ErrorAction Stop
@"
POSTGRES_PASSWORD=check-only-not-a-real-secret
TC_APP_DB_PASSWORD=check-only-not-a-real-secret
POSTGRES_PORT=15498
TC_DISCORD_OWNER_USER_ID=100000000000000001
TC_BACKUP_ROOT=$BackupRoot
"@ | Set-Content -Path $EnvFile -Encoding utf8 -ErrorAction Stop

# Ambient shell environment wins over --env-file during Compose variable
# interpolation. .github/workflows/ci.yml exports its own POSTGRES_PASSWORD
# (for an entirely unrelated Postgres service) as an ambient env var, which
# makes THAT value - not "check-only-not-a-real-secret" above - the real
# password Compose gives this throwaway project's `postgres` service. Every
# ad-hoc `docker run`/`docker compose exec` invocation below that
# authenticates as tc_migrator must resolve the password the same way
# Compose does, via this variable, rather than assuming the literal above -
# confirmed directly in CI (thought-capture run 34572786627, scripts/check-
# backup-restore.sh's own equivalent) authenticating with the wrong
# password and failing every attempt.
$ResolvedPostgresPassword = if ($env:POSTGRES_PASSWORD) { $env:POSTGRES_PASSWORD } else { "check-only-not-a-real-secret" }

function Invoke-Cleanup {
    docker rm -f $BlobWriterContainer *> $null
    # Permission handback FIRST (review finding F1). backup.sh leaves this
    # tree owned by uid 999, mode 0700; on a Linux host the invoking user
    # genuinely cannot delete inside it, so the directory would be left
    # behind - and a failing delete inside a finally block can mask the real
    # exit code too. `chmod -R a+rwX` rather than scripts/check-backup-
    # restore.sh's `chown -R $(id -u):$(id -g)`: PowerShell has no portable
    # invoking-uid to chown to, and this is a throwaway directory whose whole
    # remaining lifetime is the delete on the next line. The `backup` service
    # is the one that mounts the backup root read-write; `restore-test`
    # mounts it :ro and so cannot change it.
    #
    # `--profile core --profile backup` is REQUIRED even with `--no-deps` - a
    # second review round found this call failing outright with "no such
    # service: postgres" without it (Compose drops a profile-gated service's
    # `depends_on` target from the resolved model entirely when no matching
    # profile is active; `--no-deps` only skips STARTING a dependency, not
    # resolving whether it exists). This had been silently failing every run
    # on THIS platform without anyone noticing, because Docker Desktop for
    # Windows does not enforce the permissions this handback exists to undo
    # in the first place - the delete below still succeeded regardless of
    # whether the chmod ever ran.
    if ($BackupHasRun) {
        docker compose --env-file $EnvFile -p $BackupProject -f $ComposeFile `
            --profile core --profile backup `
            run --rm --no-deps -T --user 0:0 --entrypoint bash backup `
            -c "chmod -R a+rwX /backups" *> $null
    }
    & "$PSScriptRoot/compose-teardown.ps1" --env-file $EnvFile -p $RestoreProject `
        -f $RestoreComposeFile down -v --remove-orphans *> $null
    # --profile core --profile backup: `docker compose down` only acts on
    # services whose profile is active in the SAME invocation - every
    # service in docker-compose.yml is profile-gated, so a `down` without
    # matching --profile flags is a silent no-op that leaves every
    # container running (confirmed directly while building the bash
    # version of this script).
    & "$PSScriptRoot/compose-teardown.ps1" --env-file $EnvFile -p $BackupProject `
        -f $ComposeFile --profile core --profile backup down -v --remove-orphans *> $null
    Remove-Item -Path $EnvFile -ErrorAction SilentlyContinue
    if ($AttachmentSeedFile) { Remove-Item -Path $AttachmentSeedFile -ErrorAction SilentlyContinue }
    # Only ever remove a directory THIS run created: guarded on both a
    # non-empty variable and the marker file this run itself wrote above,
    # so a future edit that changes $BackupRoot's value earlier in the
    # script can never turn this into a recursive delete of an unrelated
    # path.
    if ($BackupRoot -and (Test-Path -LiteralPath $BackupRootMarker)) {
        Remove-Item -LiteralPath $BackupRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($AssertScriptDir -and (Test-Path -LiteralPath $AssertScriptDir)) {
        Remove-Item -LiteralPath $AssertScriptDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$ExitCode = 1
try {
    # Tear down first too, in case a prior run left either throwaway
    # project behind.
    & "$PSScriptRoot/compose-teardown.ps1" --env-file $EnvFile -p $RestoreProject `
        -f $RestoreComposeFile down -v --remove-orphans *> $null
    & "$PSScriptRoot/compose-teardown.ps1" --env-file $EnvFile -p $BackupProject `
        -f $ComposeFile --profile core --profile backup down -v --remove-orphans *> $null

    Write-Host "--- bringing up a throwaway postgres (project: $BackupProject)" -ForegroundColor Cyan
    docker compose --env-file $EnvFile -p $BackupProject -f $ComposeFile `
        --profile core --profile backup up -d --build postgres
    if ($LASTEXITCODE -ne 0) { throw "failed to start postgres (exit $LASTEXITCODE)" }

    Write-Host "--- running migrations (schema + workspace seed)" -ForegroundColor Cyan
    docker compose --env-file $EnvFile -p $BackupProject -f $ComposeFile `
        --profile core up --build migrate
    if ($LASTEXITCODE -ne 0) { throw "migrate failed (exit $LASTEXITCODE)" }

    # Seeds one synthetic file (CLAUDE.md: "use synthetic memory content in
    # fixtures") into the attachments volume before running backup, so this
    # check exercises the real attachment-copy/hash-manifest code path -
    # without this, every run before it was seeded had zero attachments,
    # and backup.sh's "no attachments were present" branch (an empty
    # manifest, nothing copied) was the ONLY path this check ever proved
    # worked. A review of this PR correctly found that gap: an empty
    # backup passing proves nothing about whether a real one, with real
    # attachments, actually works.
    $AttachmentContent = "synthetic attachment for check-backup-restore.ps1 - $([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))"
    $Sha256 = [System.Security.Cryptography.SHA256]::Create()
    $AttachmentSha256 = [System.BitConverter]::ToString(
        $Sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($AttachmentContent))
    ).Replace("-", "").ToLowerInvariant()
    # Content is written through a bind-mounted HOST temp file, not passed as
    # a `docker run`/`sh -c` argument: Windows PowerShell 5.1's native-command
    # argument serialization mishandles a string containing spaces when it
    # has to nest inside another quoted string (the sh -c script here needs
    # its own double quotes around "$0") - confirmed directly: "hello world"
    # arrived inside the container as two separate, space-stripped tokens.
    # A bind-mounted file never goes through argv encoding at all, so it is
    # immune to that class of bug. [System.Text.UTF8Encoding]::new($false):
    # Set-Content -Encoding utf8 in Windows PowerShell 5.1 always prepends a
    # UTF-8 BOM, which would silently shift every byte the container-side
    # sha256/size comparison sees - explicit no-BOM encoding avoids that.
    $AttachmentSeedFile = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllText($AttachmentSeedFile, $AttachmentContent, [System.Text.UTF8Encoding]::new($false))
    docker run --rm -v "${BackupProject}_attachments:/data" -v "${AttachmentSeedFile}:/seed.txt:ro" alpine:3.20 `
        sh -c "mkdir -p /data/ab && cp /seed.txt /data/ab/synthetic.txt"
    $AttachmentSeedExitCode = $LASTEXITCODE
    if ($AttachmentSeedExitCode -ne 0) { throw "failed to seed the synthetic attachment (exit $AttachmentSeedExitCode)" }
    # Deliberately NOT deleted here any more: the same no-BOM host file is
    # bind-mounted into the assertion container below as the expected-content
    # fixture, so the comparison is byte-for-byte against exactly what was
    # seeded, with no argv encoding anywhere in the path. Removed in
    # Invoke-Cleanup instead.

    Write-Host "--- running backup" -ForegroundColor Cyan
    $BackupHasRun = $true
    docker compose --env-file $EnvFile -p $BackupProject -f $ComposeFile `
        --profile core --profile backup up --build --force-recreate --exit-code-from backup backup
    if ($LASTEXITCODE -ne 0) { throw "backup failed (exit $LASTEXITCODE)" }

    # -------------------------------------------------------------------
    # Every assertion about the backup root runs INSIDE a container, never on
    # the host (review finding F1 - see this file's PLATFORM TRAP header
    # note). The `restore-test` service is reused because it already
    # bind-mounts $TC_BACKUP_ROOT read-only, already runs as the uid that owns
    # those files, and Compose already resolves that path correctly on both
    # platforms - which the host side does not. `--no-deps` because none of
    # these need the scratch database; `-T` because there is no TTY in CI.
    # -------------------------------------------------------------------
    function Invoke-BackupRootExec {
        # Reads only: `restore-test` mounts the backup root `:ro`
        # (deploy/compose/backup.restore-test.docker-compose.yml) - a write
        # here fails regardless of uid. Use Invoke-BackupRootWriteExec below
        # for anything that writes.
        param(
            [Parameter(Mandatory)][string]$AsUser,
            [Parameter(Mandatory)][string]$ScriptName,
            [string[]]$ExtraArgs = @()
        )
        docker compose --env-file $EnvFile -p $RestoreProject -f $RestoreComposeFile `
            run --rm --no-deps -T --user $AsUser `
            -v "${AssertScriptDir}:/assert:ro" @ExtraArgs `
            --entrypoint bash restore-test "/assert/$ScriptName"
    }

    function Invoke-BackupRootWriteExec {
        # Same calling convention, but through the `backup` service instead
        # of `restore-test`: `backup` is the only service that mounts the
        # backup root read-write. `--profile core --profile backup` is
        # required even with `--no-deps` - see Invoke-Cleanup's own use of
        # this same pattern for why.
        param(
            [Parameter(Mandatory)][string]$AsUser,
            [Parameter(Mandatory)][string]$ScriptName,
            [string[]]$ExtraArgs = @()
        )
        docker compose --env-file $EnvFile -p $BackupProject -f $ComposeFile `
            --profile core --profile backup `
            run --rm --no-deps -T --user $AsUser `
            -v "${AssertScriptDir}:/assert:ro" @ExtraArgs `
            --entrypoint bash backup "/assert/$ScriptName"
    }

    Write-AssertScript "stat-backup-root.sh" @'
stat -c '%u %a' /backups
'@

    Write-AssertScript "probe-unrelated-uid.sh" @'
if cat /backups/latest.txt >/dev/null 2>&1; then echo READABLE; else echo NOT-READABLE; fi
'@

    Write-AssertScript "write-enforcement-probe.sh" @'
echo enforcement-probe >/backups/.tc-enforcement-probe && chown 4242:4242 /backups/.tc-enforcement-probe && chmod 600 /backups/.tc-enforcement-probe && echo PROBE-WRITTEN
'@

    Write-AssertScript "read-enforcement-probe.sh" @'
if cat /backups/.tc-enforcement-probe >/dev/null 2>&1; then echo READABLE; else echo NOT-READABLE; fi
'@

    Write-AssertScript "remove-enforcement-probe.sh" @'
rm -f /backups/.tc-enforcement-probe
'@

    Write-AssertScript "assert-attachment.sh" @'
set -euo pipefail
copied="/backups/attachments/ab/synthetic.txt"
if [[ ! -f "$copied" ]]; then
  echo "FAIL: backup did not copy the seeded attachment to <backup root>/attachments/ab/synthetic.txt" >&2
  ls -la /backups /backups/attachments >&2 || true
  exit 1
fi
if ! cmp -s "$copied" /expected.txt; then
  echo "FAIL: copied attachment content does not match what was seeded" >&2
  exit 1
fi
latest_name="$(tr -d '[:space:]' </backups/latest.txt)"
manifest_name="$(grep -oE '"attachments_manifest_file": *"[^"]*"' "/backups/$latest_name" | sed -E 's/.*"([^"]+)"$/\1/')"
if [[ -z "$manifest_name" ]]; then
  echo "FAIL: could not find attachments_manifest_file in $latest_name" >&2
  exit 1
fi
if ! grep -q "^${EXPECTED_SHA256}  \./ab/synthetic\.txt$" "/backups/$manifest_name"; then
  echo "FAIL: attachment manifest $manifest_name does not record the seeded file's sha256 ($EXPECTED_SHA256)" >&2
  cat "/backups/$manifest_name" >&2
  exit 1
fi
echo "OK: seeded attachment present and hash-valid after backup"
'@

    # -------------------------------------------------------------------
    # Permission property (review finding F1, step 1; corrected by a second
    # review round after this script's own first version conflated two
    # different questions). backup.sh's closing `chmod 700` is a real
    # security property on Linux - the backup contains the full plaintext
    # database and every attachment - so assert it deliberately instead of
    # leaving it as the accident that used to break this pair of scripts. An
    # unrelated, non-root uid must not be able to read even `latest.txt`.
    #
    # The two questions, kept deliberately separate:
    #   (a) Does THIS PLATFORM's bind mount enforce POSIX ownership/mode at
    #       all?
    #   (b) Given that it does, did backup.sh actually leave the REAL backup
    #       root at the correct 999/700?
    #
    # The original version of this check collapsed both into one comparison
    # (stat the real backup root, soft-pass on anything but "999 700") -
    # which meant a real regression in backup.sh's own chown/chmod was
    # indistinguishable from "this platform doesn't enforce permissions" and
    # silently printed SKIP instead of failing. (a) is answered first,
    # independently, with a disposable scratch fixture that has nothing to do
    # with the real backup root; only once enforcement is confirmed does this
    # treat a mismatch on the real backup root as a hard failure.
    #
    # The probe file is written and chowned as ROOT (0:0) through the
    # `backup` service (the only one with a read-write mount): by this point
    # the backup root is already uid-999/mode-700 from the real backup that
    # just ran, so a non-root uid could never write into it at all regardless
    # of platform - only root's own privilege bypasses that check, the same
    # way backup.sh's own root phase does. It is then read back as an
    # UNRELATED uid (5252, neither root nor the 4242 it is chowned to)
    # through `restore-test`.
    # -------------------------------------------------------------------
    Write-Host "--- probing whether this platform enforces POSIX ownership/mode across the bind mount" -ForegroundColor Cyan
    $EnforcementProbeWritten = (Invoke-BackupRootWriteExec -AsUser "0:0" -ScriptName "write-enforcement-probe.sh" 2>$null |
        Select-Object -Last 1 | ForEach-Object { "$_".Trim() })
    if ($EnforcementProbeWritten -ne "PROBE-WRITTEN") {
        throw "could not write the permission-enforcement probe file through a container - the probe did not run"
    }
    $EnforcementHoldsVerdict = (Invoke-BackupRootExec -AsUser "5252:5252" -ScriptName "read-enforcement-probe.sh" 2>$null |
        Select-Object -Last 1 | ForEach-Object { "$_".Trim() })
    Invoke-BackupRootWriteExec -AsUser "0:0" -ScriptName "remove-enforcement-probe.sh" *> $null
    if (-not $EnforcementHoldsVerdict) {
        throw "could not read the permission-enforcement probe back through a container - the probe did not run"
    }

    Write-Host "--- asserting the backup root is unreadable to an unrelated non-root uid" -ForegroundColor Cyan
    if ($EnforcementHoldsVerdict -ne "NOT-READABLE") {
        # Question (a) answered NO: this bind mount does not enforce POSIX
        # bits for a container running as an unrelated uid at all (Docker
        # Desktop for Windows, i.e. almost every run of THIS script). The
        # real backup root's permissions cannot be asserted here - loudly
        # SKIP rather than silently pass, and rather than mistake this for a
        # regression in backup.sh, which is a separate question this
        # platform cannot answer either way. CI runs check-backup-restore.sh
        # on Linux, where it is asserted for real.
        Write-Host "SKIP: this platform does not enforce POSIX ownership/mode across the backup bind mount at all (an unrelated uid could read a freshly-created 0600 file) - the restrictive-permission property cannot be asserted here; CI on Linux does assert it"
    }
    else {
        # Question (a) answered YES: enforcement demonstrably works on this
        # platform, so a mismatch on the REAL backup root from here on is a
        # real regression in backup.sh, not a platform limitation - hard
        # failure, no soft pass.
        $BackupRootOwnership = (Invoke-BackupRootExec -AsUser "0:0" -ScriptName "stat-backup-root.sh" 2>$null |
            Select-Object -Last 1 | ForEach-Object { "$_".Trim() })
        if (-not $BackupRootOwnership) {
            throw "could not read the backup root's ownership through a container - the permission probe did not run"
        }
        if ($BackupRootOwnership -ne "999 700") {
            throw "the backup root is not uid-999/mode-700 (saw '$BackupRootOwnership') on a platform that enforces POSIX permissions - backup.sh must leave it readable only by its owner"
        }
        $UnrelatedUidVerdict = (Invoke-BackupRootExec -AsUser "4242:4242" -ScriptName "probe-unrelated-uid.sh" 2>$null |
            Select-Object -Last 1 | ForEach-Object { "$_".Trim() })
        if ($UnrelatedUidVerdict -ne "NOT-READABLE") {
            throw "uid 4242 could read the backup root ($UnrelatedUidVerdict) - backup.sh must leave it readable only by its owner"
        }
        Write-Host "OK: backup root is 0700/uid-999 and unreadable to an unrelated uid"
    }

    Write-Host "--- asserting the seeded attachment was actually copied and hashed" -ForegroundColor Cyan
    Invoke-BackupRootExec -AsUser "postgres" -ScriptName "assert-attachment.sh" -ExtraArgs @(
        "-v", "${AttachmentSeedFile}:/expected.txt:ro",
        "-e", "EXPECTED_SHA256=$AttachmentSha256"
    ) | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "the container-side backup-root assertions failed (exit $LASTEXITCODE) - see above" }

    # A fresh postgres-scratch every round, not reused across rounds - the
    # same bug scripts/restore-test.ps1's own comment/commit explains
    # applies equally between the rounds run here.
    function Invoke-RestoreTest {
        # A PowerShell function's return value is the aggregate of
        # EVERYTHING written to its output stream during execution, not
        # only its explicit `return` - an uncaptured native command's own
        # stdout leaks in too. Confirmed directly: without `| Out-Host`
        # here, `$ExitCode = Invoke-RestoreTest` below assigned an array
        # mixing every line restore-test.sh printed with the exit code, and
        # `if ($ExitCode -ne 0)` against that array evaluated true even on
        # a genuine success (0 exit code), because most of the OTHER array
        # elements - ordinary log lines - are, as strings, "not equal to
        # 0". `Out-Host` still prints the output live; it just does not
        # feed it back into the pipeline.
        docker compose --env-file $EnvFile -p $RestoreProject -f $RestoreComposeFile `
            down -v --remove-orphans *> $null
        docker compose --env-file $EnvFile -p $RestoreProject -f $RestoreComposeFile `
            up -d --build --force-recreate --wait postgres-scratch | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "failed to start postgres-scratch (exit $LASTEXITCODE)" }
        docker compose --env-file $EnvFile -p $RestoreProject -f $RestoreComposeFile `
            run --rm restore-test | Out-Host
        return $LASTEXITCODE
    }

    Write-Host "--- round 1: restore-test against a complete backup (expect success)" -ForegroundColor Cyan
    $ExitCode = Invoke-RestoreTest
    if ($ExitCode -ne 0) { throw "round 1 restore-test failed (exit $ExitCode), but the backup was complete - expected success" }

    # -------------------------------------------------------------------
    # Race regression (review finding, Finding 3): backup.sh used to take
    # its pg_dump snapshot, then count rows through SEPARATE, later
    # connections. A capture committed in between made the recorded count
    # higher than the dump actually held, and restore-test.sh failed a
    # perfectly valid backup. This starts a continuous writer BEFORE the
    # backup begins and keeps it running for the backup's entire duration
    # (not a single, hard-to-time insert), so the race window is covered
    # regardless of exactly how long pg_dump takes - on the old code this
    # reliably fails restore-test; on the snapshot-consistent code
    # (backup.sh now exports one snapshot and counts rows in that same
    # still-open transaction) it reliably passes.
    #
    # Deliberately runs as round 2, BEFORE the orphan-blob negative test
    # below: that test permanently inserts a `blobs` row with no backing
    # file into $BackupProject's database (never rolled back), and this
    # round needs a database that isn't already broken that way - confirmed
    # directly while building this: running it after the negative test
    # failed restore-test for the orphan blob's real, pre-existing reason,
    # not for anything to do with the race itself.
    # -------------------------------------------------------------------
    Write-Host "--- round 2: concurrent writer during backup (race regression)" -ForegroundColor Cyan
    # The job writes one integer to its output stream per successfully
    # COMMITted insert (review finding: the prior version never checked the
    # psql exit code, so a writer failing on every attempt - e.g. a
    # schema/constraint mismatch - would silently pass this round without
    # ever having exercised the race). 'api' is a real allowed
    # `thoughts.source` value (migrations/versions/
    # 0001_canonical_capture_layer.py's CHECK); the previous 'race-test'
    # value violated that CHECK, so every insert was failing.
    $WriterJob = Start-Job -ScriptBlock {
        param($EnvFile, $BackupProject, $ComposeFile)
        $i = 0
        while ($true) {
            $i++
            $sql = "INSERT INTO thoughts (workspace_id, author_user_id, source, source_message_id, body, client_created_at, client_timezone, client_local_date, client_local_time, received_at, content_language) SELECT w.id, u.id, 'api', 'race-test-' || $i || '-' || extract(epoch from clock_timestamp()), 'synthetic race-regression thought', now(), 'UTC', current_date, current_time, now(), 'en' FROM workspaces w JOIN users u ON true LIMIT 1"
            docker compose --env-file $EnvFile -p $BackupProject -f $ComposeFile `
                exec -T -e "PGPASSWORD=$ResolvedPostgresPassword" postgres `
                psql -v ON_ERROR_STOP=1 --quiet -U tc_migrator -d thought_capture -c $sql *> $null
            if ($LASTEXITCODE -eq 0) { Write-Output $i }
            Start-Sleep -Milliseconds 100
        }
    } -ArgumentList $EnvFile, $BackupProject, $ComposeFile

    # -------------------------------------------------------------------
    # Blob-ordering regression (review finding F3). The `thoughts` writer
    # above only ever exercised row counts; it never touched the attachments
    # volume, so it could not detect that backup.sh used to copy attachments
    # BEFORE exporting the snapshot pg_dump reads through. A blob written and
    # committed in that window is in the dump but not in the backup directory.
    #
    # This writer does the real thing, in blob-store order (write and sync the
    # file, THEN commit the `blobs` row - the same order
    # packages/infrastructure/src/tc_infrastructure/storage/blob_store.py's
    # `put` guarantees, and the order the fix's correctness argument depends
    # on), for the backup's whole duration. On the old copy-first ordering this
    # reliably leaves the restored database referencing a blob the backup never
    # copied, which restore-test.sh's "every database-referenced blob" step
    # then fails on, naming its sha256.
    #
    # One long-lived container rather than a `docker run` per iteration:
    # container startup dominates otherwise, and a writer that manages one or
    # two blobs over the whole backup is not a race test. The loop body is
    # bind-mounted as a file for the same argv-encoding reason as the assertion
    # scripts above.
    # -------------------------------------------------------------------
    Write-AssertScript "blob-writer.sh" @'
set -u
i=0
while true; do
  i=$((i + 1))
  content="synthetic race blob $i $(date -u +%s%N)"
  sha="$(printf '%s' "$content" | sha256sum | cut -d' ' -f1)"
  size="$(printf '%s' "$content" | wc -c)"
  # Matches blob_store.py's own `storage_key_for`: two 2-character fan-out
  # levels, then the full hash.
  key="${sha:0:2}/${sha:2:2}/$sha"
  mkdir -p "/data/attachments/${sha:0:2}/${sha:2:2}"
  # File first, durably, THEN the row - blob_store.py's `put` order, and the
  # premise of the fix under test.
  printf '%s' "$content" >"/data/attachments/$key"
  sync
  # Diagnostic (review finding, fourth round): capture and periodically
  # print psql's stderr on failure instead of swallowing it entirely - a
  # writer failing on every single attempt (wrong host, wrong role, wrong
  # password, schema drift) used to produce the exact same empty log as one
  # that simply has not run yet.
  if psql_stderr="$(psql -v ON_ERROR_STOP=1 --quiet -h postgres -U tc_migrator -d thought_capture -c "INSERT INTO blobs (sha256, size_bytes, media_type, storage_key) VALUES ('$sha', $size, 'text/plain', '$key')" 2>&1 >/dev/null)"; then
    echo "COMMITTED $sha"
  elif [[ $((i % 20)) -eq 1 ]]; then
    echo "INSERT_FAILED (attempt $i): $psql_stderr" >&2
  fi
  sleep 0.05
done
'@

    docker rm -f $BlobWriterContainer *> $null
    docker run -d --name $BlobWriterContainer `
        --network "${BackupProject}_default" `
        -v "${BackupProject}_attachments:/data/attachments" `
        -v "${AssertScriptDir}:/assert:ro" `
        -e "PGPASSWORD=$ResolvedPostgresPassword" `
        postgres:18.6-trixie@sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280 `
        bash /assert/blob-writer.sh *> $null
    if ($LASTEXITCODE -ne 0) { throw "failed to start the concurrent blob writer (exit $LASTEXITCODE)" }

    # Wait for the writer's first COMMITTED blob before starting the backup
    # (review finding, third round). The backup this exercises now finishes
    # in well under a second (the attachment copy runs concurrently with
    # pg_dump, not serially after it), which is faster than `docker run -d`
    # above can boot a fresh container and get its first psql INSERT
    # committed - so starting the backup immediately could lose the race
    # outright, passing zero commits without ever having exercised anything.
    # Blocking here until the writer is demonstrably already committing -
    # bounded, so a genuinely broken writer still fails fast via the
    # existing post-backup BlobCommitCount check below - makes the race
    # actually start under load instead of racing container startup itself.
    $WriterReady = $false
    for ($i = 0; $i -lt 30; $i++) {
        $probeLog = (docker logs $BlobWriterContainer 2>&1 | Out-String)
        if ($probeLog -match '(?m)^COMMITTED ') { $WriterReady = $true; break }
        $running = (docker ps -q -f "name=^$BlobWriterContainer`$")
        if (-not $running) { break }
        Start-Sleep -Milliseconds 200
    }
    if (-not $WriterReady) {
        Write-Host (docker logs $BlobWriterContainer 2>&1 | Out-String)
        throw "the concurrent blob writer never committed a blob within 6s of starting - it may have failed to start or connect"
    }

    try {
        docker compose --env-file $EnvFile -p $BackupProject -f $ComposeFile `
            --profile core --profile backup up --build --force-recreate --exit-code-from backup backup
        if ($LASTEXITCODE -ne 0) { throw "round 2 backup failed (exit $LASTEXITCODE)" }
    }
    finally {
        Stop-Job -Job $WriterJob -ErrorAction SilentlyContinue
        docker stop -t 2 $BlobWriterContainer *> $null
    }

    # Same guard as the `thoughts` writer's, for the same reason: a blob writer
    # that failed on every iteration - a schema change, a wrong password, an
    # unreachable host - would make this round pass while testing nothing.
    $BlobWriterLog = (docker logs $BlobWriterContainer 2>&1 | Out-String)
    docker rm -f $BlobWriterContainer *> $null
    $BlobCommitCount = ([regex]::Matches($BlobWriterLog, '(?m)^COMMITTED ')).Count
    if ($BlobCommitCount -lt 1) {
        Write-Host $BlobWriterLog
        throw "the concurrent blob writer never committed a blob during the backup - the copy-ordering regression is not actually exercised"
    }
    Write-Host "--- round 2: concurrent blob writer committed $BlobCommitCount blob(s) during the backup"

    # Asserts the race was actually exercised, not merely that the backup
    # succeeded - a writer that never committed anything would make this
    # round pass trivially without ever having started a live snapshot
    # against a moving target.
    $WriterCommitCount = (Receive-Job -Job $WriterJob -ErrorAction SilentlyContinue | Measure-Object).Count
    Remove-Job -Job $WriterJob -Force -ErrorAction SilentlyContinue
    if ($WriterCommitCount -lt 1) { throw "the concurrent writer never committed a row during the backup - race regression not actually exercised" }
    Write-Host "--- round 2: concurrent writer committed $WriterCommitCount row(s) during the backup"

    Write-Host "--- round 2: restore-test against the concurrently-written backup (expect success)" -ForegroundColor Cyan
    $ExitCode = Invoke-RestoreTest
    if ($ExitCode -ne 0) { throw "round 2 restore-test failed (exit $ExitCode) against a backup taken while a writer committed rows concurrently" }
    Write-Host "OK: restore-test passed against a backup taken while a writer committed rows concurrently"

    # -------------------------------------------------------------------
    # Negative test (review finding): restore-test.sh's manifest re-check
    # can only ever re-verify files that WERE copied - a copy step that
    # silently omits a blob the database still references would produce a
    # manifest with nothing to complain about, and restore-test would pass
    # while the restored `blobs` row points at a file that exists nowhere
    # in the backup. Proves deploy/compose/backup/restore-test.sh's "every
    # database-referenced blob" check (queries the restored `blobs` table
    # directly, independent of the manifest) actually catches that.
    #
    # Simulates the omission the cheap way: insert a `blobs` row for
    # content that was never written to the attachments volume at all,
    # rather than reproducing an actual copy bug. From restore-test.sh's
    # point of view the two are indistinguishable.
    #
    # Deliberately runs LAST: this permanently corrupts $BackupProject's
    # database (the orphan `blobs` row is never rolled back), so nothing
    # after it may depend on a clean database.
    # -------------------------------------------------------------------
    Write-Host "--- round 3: seeding a database-referenced blob with no backing file (negative test)" -ForegroundColor Cyan
    $OrphanContent = "synthetic orphan blob for check-backup-restore.ps1's negative test - never written to attachments - $([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))"
    $OrphanSha256 = [System.BitConverter]::ToString(
        $Sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($OrphanContent))
    ).Replace("-", "").ToLowerInvariant()
    $OrphanSize = [System.Text.Encoding]::UTF8.GetByteCount($OrphanContent)
    # Matches packages/infrastructure/src/tc_infrastructure/storage/blob_store.py's
    # own `storage_key_for`: two 2-character fan-out levels, then the full hash.
    $OrphanStorageKey = "$($OrphanSha256.Substring(0,2))/$($OrphanSha256.Substring(2,2))/$OrphanSha256"
    docker compose --env-file $EnvFile -p $BackupProject -f $ComposeFile `
        exec -T -e "PGPASSWORD=$ResolvedPostgresPassword" postgres `
        psql -v ON_ERROR_STOP=1 --quiet -U tc_migrator -d thought_capture -c `
        "INSERT INTO blobs (sha256, size_bytes, media_type, storage_key) VALUES ('$OrphanSha256', $OrphanSize, 'text/plain', '$OrphanStorageKey')"
    if ($LASTEXITCODE -ne 0) { throw "failed to seed the orphan blobs row (exit $LASTEXITCODE)" }

    Write-Host "--- round 3: running backup again (the dump now references the orphan blob)" -ForegroundColor Cyan
    docker compose --env-file $EnvFile -p $BackupProject -f $ComposeFile `
        --profile core --profile backup up --build --force-recreate --exit-code-from backup backup
    if ($LASTEXITCODE -ne 0) { throw "round 3 backup failed (exit $LASTEXITCODE)" }

    Write-Host "--- round 3: running restore-test against the orphan-blob backup (expect FAILURE)" -ForegroundColor Cyan
    $Round3Output = & {
        docker compose --env-file $EnvFile -p $RestoreProject -f $RestoreComposeFile `
            down -v --remove-orphans *> $null
        docker compose --env-file $EnvFile -p $RestoreProject -f $RestoreComposeFile `
            up -d --build --force-recreate --wait postgres-scratch 2>&1
        docker compose --env-file $EnvFile -p $RestoreProject -f $RestoreComposeFile `
            run --rm restore-test 2>&1
    } | Out-String
    $Round3ExitCode = $LASTEXITCODE
    Write-Host $Round3Output

    if ($Round3ExitCode -eq 0) {
        throw "restore-test succeeded against a backup missing a database-referenced blob (sha256=$OrphanSha256) - it should have failed"
    }
    if ($Round3Output -notmatch [regex]::Escape($OrphanSha256)) {
        throw "restore-test failed (exit $Round3ExitCode), as expected, but its output never named the missing blob (sha256=$OrphanSha256) - the failure may be for the wrong reason"
    }
    Write-Host "OK: restore-test correctly failed (exit $Round3ExitCode) on a database-referenced blob missing from the backup"
    $ExitCode = 0
}
catch {
    Write-Host "FAIL: $_" -ForegroundColor Red
    $ExitCode = 1
}
finally {
    Invoke-Cleanup
}

exit $ExitCode
