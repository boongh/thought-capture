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
$EnvFile = ".env"

# Must match deploy/compose/docker-compose.yml's own `name:` field - the same
# constant scripts/compose-teardown.ps1 pins, for the same reason.
$RealProjectName = "thought-capture"

# A backup must target the REAL project, and must be able to prove it did
# (review finding, PR #32). Compose resolves a project name in this order:
# `-p` beats COMPOSE_PROJECT_NAME, which beats the compose file's own `name:`.
# This script originally passed no `-p` at all and relied on
# `name: thought-capture` - so an operator with COMPOSE_PROJECT_NAME set for a
# second stack, or set in .env (which `--env-file` makes Compose read for
# exactly this variable), got a green, successful backup of a different,
# probably empty stack. Pinned `-p` on every call below AND a fail-closed
# refusal on a conflicting override, for the reasons scripts/backup.sh's own
# comment spells out at length.
function Write-ProjectOverrideRefusal {
    param([string]$Source, [string]$Value)
    Write-Host "Refusing: $Source sets COMPOSE_PROJECT_NAME=""$Value"", but a backup only ever" -ForegroundColor Red
    Write-Host "targets this repository's real Compose project (""$RealProjectName"")." -ForegroundColor Red
    Write-Host ""
    Write-Host "Running anyway would either back up a different stack or report success" -ForegroundColor Red
    Write-Host "over an empty one. Remove or correct that setting, then run this again." -ForegroundColor Red
}

if ($env:COMPOSE_PROJECT_NAME -and $env:COMPOSE_PROJECT_NAME -ne $RealProjectName) {
    Write-ProjectOverrideRefusal -Source "the environment" -Value $env:COMPOSE_PROJECT_NAME
    exit 1
}

# The env file as well as the ambient environment - see scripts/backup.sh's
# comment on the same check. Last assignment wins, matching how Compose reads
# the file; the regex trims surrounding whitespace and the value is then
# stripped of surrounding quotes.
if (Test-Path $EnvFile) {
    $EnvFileProjectName = $null
    foreach ($Line in (Get-Content $EnvFile)) {
        if ($Line -match '^\s*COMPOSE_PROJECT_NAME\s*=\s*(.*?)\s*$') {
            $EnvFileProjectName = $Matches[1].Trim('"').Trim("'")
        }
    }
    if ($EnvFileProjectName -and $EnvFileProjectName -ne $RealProjectName) {
        Write-ProjectOverrideRefusal -Source $EnvFile -Value $EnvFileProjectName
        exit 1
    }
}

Write-Host "--- ensuring postgres is up (core profile)" -ForegroundColor Cyan
docker compose --env-file $EnvFile -p $RealProjectName -f $ComposeFile `
  --profile core --profile backup up -d postgres
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "--- running backup" -ForegroundColor Cyan
docker compose --env-file $EnvFile -p $RealProjectName -f $ComposeFile `
  --profile core --profile backup `
  up --build --force-recreate --exit-code-from backup @args backup
exit $LASTEXITCODE
