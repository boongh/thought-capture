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
#>


# Deliberately NOT $ErrorActionPreference = "Stop": with it set, Windows
# PowerShell 5.1 turns any line `docker compose` writes to stderr into a
# terminating error - even on exit code 0. Confirmed directly while testing
# scripts/compose-teardown.ps1: a real, successful `docker compose` call
# that only warned still crashed the script. Real failures are still caught
# below via explicit $LASTEXITCODE checks.

Set-Location (Join-Path $PSScriptRoot "..") -ErrorAction Stop

$BackupProject = "thought-capture-check-backup"
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

$EnvFile = New-TemporaryFile -ErrorAction Stop
@"
POSTGRES_PASSWORD=check-only-not-a-real-secret
TC_APP_DB_PASSWORD=check-only-not-a-real-secret
POSTGRES_PORT=15498
TC_DISCORD_OWNER_USER_ID=100000000000000001
TC_BACKUP_ROOT=$BackupRoot
"@ | Set-Content -Path $EnvFile -Encoding utf8 -ErrorAction Stop

function Invoke-Cleanup {
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
    # Only ever remove a directory THIS run created: guarded on both a
    # non-empty variable and the marker file this run itself wrote above,
    # so a future edit that changes $BackupRoot's value earlier in the
    # script can never turn this into a recursive delete of an unrelated
    # path.
    if ($BackupRoot -and (Test-Path -LiteralPath $BackupRootMarker)) {
        Remove-Item -LiteralPath $BackupRoot -Recurse -Force -ErrorAction SilentlyContinue
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
    Remove-Item -Path $AttachmentSeedFile -ErrorAction SilentlyContinue
    if ($AttachmentSeedExitCode -ne 0) { throw "failed to seed the synthetic attachment (exit $AttachmentSeedExitCode)" }

    Write-Host "--- running backup" -ForegroundColor Cyan
    docker compose --env-file $EnvFile -p $BackupProject -f $ComposeFile `
        --profile core --profile backup up --build --force-recreate --exit-code-from backup backup
    if ($LASTEXITCODE -ne 0) { throw "backup failed (exit $LASTEXITCODE)" }

    Write-Host "--- asserting the seeded attachment was actually copied and hashed" -ForegroundColor Cyan
    $CopiedAttachment = Join-Path $BackupRoot "attachments/ab/synthetic.txt"
    if (-not (Test-Path -LiteralPath $CopiedAttachment)) {
        throw "backup did not copy the seeded attachment to $CopiedAttachment"
    }
    $CopiedContent = Get-Content -Raw -LiteralPath $CopiedAttachment
    if ($CopiedContent -ne $AttachmentContent) {
        throw "copied attachment content does not match what was seeded"
    }
    $LatestManifestName = (Get-Content -Raw -LiteralPath (Join-Path $BackupRoot "latest.txt")).Trim()
    $LatestManifest = Get-Content -Raw -LiteralPath (Join-Path $BackupRoot $LatestManifestName)
    if ($LatestManifest -notmatch '"attachments_manifest_file":\s*"([^"]+)"') {
        throw "could not find attachments_manifest_file in $LatestManifestName"
    }
    $AttachmentsManifestFile = Join-Path $BackupRoot $Matches[1]
    $ManifestLines = Get-Content -LiteralPath $AttachmentsManifestFile
    if (-not ($ManifestLines -match "^$AttachmentSha256  \./ab/synthetic\.txt$")) {
        Write-Host ($ManifestLines -join "`n")
        throw "attachment manifest $AttachmentsManifestFile does not record the seeded file's sha256 ($AttachmentSha256)"
    }
    Write-Host "OK: seeded attachment present and hash-valid after backup"

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
                exec -T -e PGPASSWORD=check-only-not-a-real-secret postgres `
                psql -v ON_ERROR_STOP=1 --quiet -U tc_migrator -d thought_capture -c $sql *> $null
            if ($LASTEXITCODE -eq 0) { Write-Output $i }
            Start-Sleep -Milliseconds 100
        }
    } -ArgumentList $EnvFile, $BackupProject, $ComposeFile

    try {
        docker compose --env-file $EnvFile -p $BackupProject -f $ComposeFile `
            --profile core --profile backup up --build --force-recreate --exit-code-from backup backup
        if ($LASTEXITCODE -ne 0) { throw "round 2 backup failed (exit $LASTEXITCODE)" }
    }
    finally {
        Stop-Job -Job $WriterJob -ErrorAction SilentlyContinue
    }

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
        exec -T -e PGPASSWORD=check-only-not-a-real-secret postgres `
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
