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
    $AttachmentContent = "synthetic attachment for check-backup-restore.ps1 - $(Get-Date -AsUTC -Format 'yyyyMMddTHHmmssZ')"
    $Sha256 = [System.Security.Cryptography.SHA256]::Create()
    $AttachmentSha256 = [System.BitConverter]::ToString(
        $Sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($AttachmentContent))
    ).Replace("-", "").ToLowerInvariant()
    docker run --rm -v "${BackupProject}_attachments:/data" alpine:3.20 `
        sh -c 'mkdir -p /data/ab && printf "%s" "$0" > /data/ab/synthetic.txt' "$AttachmentContent"
    if ($LASTEXITCODE -ne 0) { throw "failed to seed the synthetic attachment (exit $LASTEXITCODE)" }

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

    Write-Host "--- starting postgres-scratch (project: $RestoreProject)" -ForegroundColor Cyan
    docker compose --env-file $EnvFile -p $RestoreProject -f $RestoreComposeFile `
        up -d --build --force-recreate --wait postgres-scratch
    if ($LASTEXITCODE -ne 0) { throw "failed to start postgres-scratch (exit $LASTEXITCODE)" }

    Write-Host "--- running restore-test" -ForegroundColor Cyan
    docker compose --env-file $EnvFile -p $RestoreProject -f $RestoreComposeFile `
        run --rm restore-test
    $ExitCode = $LASTEXITCODE
}
catch {
    Write-Host "FAIL: $_" -ForegroundColor Red
    $ExitCode = 1
}
finally {
    Invoke-Cleanup
}

exit $ExitCode
