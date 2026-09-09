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

$EnvFile = New-TemporaryFile -ErrorAction Stop
@"
POSTGRES_PASSWORD=check-only-not-a-real-secret
TC_APP_DB_PASSWORD=check-only-not-a-real-secret
POSTGRES_PORT=15498
TC_DISCORD_OWNER_USER_ID=100000000000000001
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
    $BackupRoot = if ($env:TC_BACKUP_ROOT) { $env:TC_BACKUP_ROOT } else { "deploy/compose/backups" }
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
    # applies equally between the two rounds run here.
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
    # -------------------------------------------------------------------
    Write-Host "--- round 2: seeding a database-referenced blob with no backing file (negative test)" -ForegroundColor Cyan
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

    Write-Host "--- round 2: running backup again (the dump now references the orphan blob)" -ForegroundColor Cyan
    docker compose --env-file $EnvFile -p $BackupProject -f $ComposeFile `
        --profile core --profile backup up --build --force-recreate --exit-code-from backup backup
    if ($LASTEXITCODE -ne 0) { throw "round 2 backup failed (exit $LASTEXITCODE)" }

    Write-Host "--- round 2: running restore-test against the orphan-blob backup (expect FAILURE)" -ForegroundColor Cyan
    $Round2Output = & {
        docker compose --env-file $EnvFile -p $RestoreProject -f $RestoreComposeFile `
            down -v --remove-orphans *> $null
        docker compose --env-file $EnvFile -p $RestoreProject -f $RestoreComposeFile `
            up -d --build --force-recreate --wait postgres-scratch 2>&1
        docker compose --env-file $EnvFile -p $RestoreProject -f $RestoreComposeFile `
            run --rm restore-test 2>&1
    } | Out-String
    $Round2ExitCode = $LASTEXITCODE
    Write-Host $Round2Output

    if ($Round2ExitCode -eq 0) {
        throw "restore-test succeeded against a backup missing a database-referenced blob (sha256=$OrphanSha256) - it should have failed"
    }
    if ($Round2Output -notmatch [regex]::Escape($OrphanSha256)) {
        throw "restore-test failed (exit $Round2ExitCode), as expected, but its output never named the missing blob (sha256=$OrphanSha256) - the failure may be for the wrong reason"
    }
    Write-Host "OK: restore-test correctly failed (exit $Round2ExitCode) on a database-referenced blob missing from the backup"
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
