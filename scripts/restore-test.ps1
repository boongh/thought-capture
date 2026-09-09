<#
.SYNOPSIS
Validates the most recent scripts/backup.ps1 backup by actually restoring it
into a throwaway, isolated Postgres.

.DESCRIPTION
Uses deploy/compose/backup.restore-test.docker-compose.yml, project
`thought-capture-restore-test` - never `thought-capture` - and checks
schema/constraints, every table's row count, and every attachment's blob
hash. See deploy/compose/backup/restore-test.sh for what "validates" means
exactly.

Always tears the scratch stack down on exit (success or failure) via
scripts/compose-teardown.ps1, itself passed the throwaway project name
explicitly - dogfooding the same guard docs/incidents/0001-... introduced,
on the one script in this repository most likely to run unattended.

.EXAMPLE
scripts/restore-test.ps1
#>


# Deliberately NOT $ErrorActionPreference = "Stop": with it set, Windows
# PowerShell 5.1 turns any line `docker compose` writes to stderr into a
# terminating error - even on exit code 0. Confirmed directly while testing
# scripts/compose-teardown.ps1: a real, successful `docker compose` call
# that only warned still crashed the script. Real failures are still caught
# below via explicit $LASTEXITCODE checks.

Set-Location (Join-Path $PSScriptRoot "..") -ErrorAction Stop

$ComposeFile = "deploy/compose/backup.restore-test.docker-compose.yml"
$RestoreTestProject = "thought-capture-restore-test"

$RestoreTestExitCode = 1
try {
    # Tear down BEFORE starting too, not only in `finally`: a prior run that
    # crashed before its own cleanup could leave a stale postgres-scratch
    # behind, and `up -d` alone reuses an already-running container
    # unchanged - confirmed directly while testing this script: a second
    # restore-test run against a still-running postgres-scratch from a first,
    # uncleaned run failed with "already exists" errors, because it was
    # never actually empty.
    #
    # --env-file .env is required even for `down`: Compose interpolates
    # every service's environment in the whole file before running any
    # command, and postgres-scratch's POSTGRES_PASSWORD uses `:?`
    # (required) - omitting it fails the teardown itself and leaves the
    # scratch container running, confirmed directly while testing the bash
    # version of this script.
    Write-Host "--- ensuring no stale restore-test stack remains (project: $RestoreTestProject)" -ForegroundColor Cyan
    & "$PSScriptRoot/compose-teardown.ps1" --env-file .env -p $RestoreTestProject -f $ComposeFile down -v --remove-orphans

    Write-Host "--- starting postgres-scratch (project: $RestoreTestProject)" -ForegroundColor Cyan
    docker compose --env-file .env -p $RestoreTestProject -f $ComposeFile up -d --build --force-recreate --wait postgres-scratch
    if ($LASTEXITCODE -ne 0) { throw "failed to start postgres-scratch (exit $LASTEXITCODE)" }

    Write-Host "--- running restore-test" -ForegroundColor Cyan
    docker compose --env-file .env -p $RestoreTestProject -f $ComposeFile run --rm restore-test
    $RestoreTestExitCode = $LASTEXITCODE
}
catch {
    Write-Host "FAIL: $_" -ForegroundColor Red
    $RestoreTestExitCode = 1
}
finally {
    Write-Host "--- tearing down the restore-test stack (project: $RestoreTestProject)" -ForegroundColor Cyan
    & "$PSScriptRoot/compose-teardown.ps1" --env-file .env -p $RestoreTestProject -f $ComposeFile down -v --remove-orphans
}

exit $RestoreTestExitCode
