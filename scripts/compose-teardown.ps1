<#
.SYNOPSIS
A guarded wrapper around `docker compose down`.

.DESCRIPTION
Written after docs/incidents/0001-docker-compose-down-deleted-the-real-dev-stack.md:
an unscoped `docker compose down -v`, run during manual verification, fell
back to this repo's own default project name (docker-compose.yml's own
`name: thought-capture`) and deleted the real, already-running local
development stack's Postgres and attachments volumes. "Remember to pass
-p" is not a control - this is.

Refuses to run at all without an explicit -p/--project-name. Refuses,
with no override, to run a volume-destroying teardown (-v/--volumes)
against this repo's real project name - that name is reserved for the
persistent local development stack and must never be torn down by an
automated or exploratory command. There is deliberately no bypass flag:
the underlying `docker compose -p thought-capture down -v` command is
still available directly, unwrapped, for the rare legitimate case of
intentionally wiping the real stack's data - this script only removes it
as an easy default.

Deliberately reads the automatic `$args` variable rather than declaring a
`param()` block with `ValueFromRemainingArguments`: PowerShell's own
parameter-binding heuristics silently swallow short flags like `-p`/`-v`
when combined with that pattern (confirmed while testing this script -
`-p thought-capture down -v` arrived with only `down` left in the bound
array). Reading `$args` directly bypasses parameter binding entirely.

Also matches `-v=<value>`/`--volumes=<value>`, not just the bare
`-v`/`--volumes` an earlier version of this script only matched - Docker's
own flag parser (Cobra/pflag) accepts that form too, and `docker compose
down --volumes=true`/`-v=true` were confirmed empirically to delete a real
named volume while going undetected by an exact-string match. Fails closed:
any `=`-form value other than pflag's own recognized falsy spellings
(matching Go's strconv.ParseBool - 0/f/F/false/False/FALSE) is treated as
volume-destroying, and the flag is only ever escalated true, never
downgraded back to false by a later token.

.EXAMPLE
scripts/compose-teardown.ps1 -p tc-scratch-1234 down -v
#>

$ErrorActionPreference = "Stop"

# Must match deploy/compose/docker-compose.yml's own `name:` field.
$RealProjectName = "thought-capture"

function Test-Falsy {
    param([string] $Value)
    return $Value -in @("0", "f", "F", "false", "False", "FALSE")
}

$ProjectName = $null
$HasVolumesFlag = $false
$PassThroughArgs = @()

$i = 0
while ($i -lt $args.Count) {
    $current = $args[$i]
    switch -Regex ($current) {
        '^(-p|--project-name)$' {
            $ProjectName = $args[$i + 1]
            $PassThroughArgs += $args[$i], $args[$i + 1]
            $i++
        }
        '^(-p|--project-name)=(.*)$' {
            $ProjectName = $Matches[2]
            $PassThroughArgs += $current
        }
        '^(-v|--volumes)$' {
            $HasVolumesFlag = $true
            $PassThroughArgs += $current
        }
        '^(-v|--volumes)=(.*)$' {
            if (-not (Test-Falsy $Matches[2])) {
                $HasVolumesFlag = $true
            }
            $PassThroughArgs += $current
        }
        default {
            $PassThroughArgs += $current
        }
    }
    $i++
}

if (-not $ProjectName) {
    Write-Host "Refusing: no -p/--project-name given." -ForegroundColor Red
    Write-Host "A bare 'docker compose down' targets this repo's default project name" -ForegroundColor Red
    Write-Host "(`"$RealProjectName`"), which is reserved for the real, persistent local" -ForegroundColor Red
    Write-Host "development stack. Pass an explicit, unique -p <name> for throwaway or" -ForegroundColor Red
    Write-Host "manual verification instead, e.g.:" -ForegroundColor Red
    Write-Host "  scripts/compose-teardown.ps1 -p tc-scratch-$(Get-Date -UFormat %s) down -v" -ForegroundColor Red
    exit 1
}

if ($ProjectName -eq $RealProjectName -and $HasVolumesFlag) {
    Write-Host "Refusing: -v/--volumes against project `"$RealProjectName`" would delete the real" -ForegroundColor Red
    Write-Host "local development stack's data volumes (postgres-data, attachments)." -ForegroundColor Red
    Write-Host "This is the exact incident recorded in" -ForegroundColor Red
    Write-Host "docs/incidents/0001-docker-compose-down-deleted-the-real-dev-stack.md." -ForegroundColor Red
    Write-Host ""
    Write-Host "If you genuinely mean to wipe the real dev stack's data, run the" -ForegroundColor Red
    Write-Host "underlying command directly and explicitly - this wrapper will not do" -ForegroundColor Red
    Write-Host "it for you:" -ForegroundColor Red
    Write-Host "  docker compose -p $RealProjectName down -v" -ForegroundColor Red
    exit 1
}

Write-Host "--- docker compose down (project: $ProjectName)" -ForegroundColor Cyan
& docker compose @PassThroughArgs
exit $LASTEXITCODE
