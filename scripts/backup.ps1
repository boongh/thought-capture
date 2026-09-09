<#
.SYNOPSIS
Runs a one-shot local backup: pg_dump (custom format, atomic write) plus an
attachment manifest/incremental copy.

.DESCRIPTION
Writes to TC_BACKUP_ROOT (default ./backups relative to deploy/compose/) - a
HOST directory, never a Docker named volume
(docs/incidents/0001-docker-compose-down-deleted-the-real-dev-stack.md is why
that distinction matters). See deploy/compose/backup/backup.sh for the actual
logic this runs.

.EXAMPLE
scripts/backup.ps1
#>


# Deliberately NOT $ErrorActionPreference = "Stop": with it set, Windows
# PowerShell 5.1 turns any line `docker compose` writes to stderr into a
# terminating error - even on exit code 0, and even for routine warnings
# (e.g. an unset optional variable). Confirmed directly while testing
# scripts/compose-teardown.ps1: a real, successful `docker compose` call
# that only warned still crashed the script. Real failures are still caught
# below via explicit $LASTEXITCODE checks, which reflect docker's own exit
# code regardless of this setting.

Set-Location (Join-Path $PSScriptRoot "..") -ErrorAction Stop

$ComposeFile = "deploy/compose/docker-compose.yml"

Write-Host "--- ensuring postgres is up (core profile)" -ForegroundColor Cyan
docker compose --env-file .env -f $ComposeFile --profile core --profile backup up -d postgres
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "--- running backup" -ForegroundColor Cyan
docker compose --env-file .env -f $ComposeFile --profile core --profile backup `
  up --build --force-recreate --exit-code-from backup @args backup
exit $LASTEXITCODE
