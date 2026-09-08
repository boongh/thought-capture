$ErrorActionPreference = "Stop"

$RepositoryRoot = Split-Path -Parent $PSScriptRoot
Push-Location $RepositoryRoot

# Steps that could not run are reported explicitly and never counted as a pass.
# See CLAUDE.md: "Never bypass or weaken a failing check to claim completion."
$Script:Skipped = @()

function Resolve-Uv {
    $onPath = Get-Command uv -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }

    # uv is commonly installed into the interpreter's Scripts directory, which
    # is not always on PATH on Windows. Ask the interpreter where that is rather
    # than guessing from the launcher's location, which may be a shim.
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $scriptsDir = & python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
        if ($LASTEXITCODE -eq 0 -and $scriptsDir) {
            $candidate = Join-Path $scriptsDir.Trim() "uv.exe"
            if (Test-Path -LiteralPath $candidate) { return $candidate }
        }
    }
    throw "uv was not found. Install it with: python -m pip install uv"
}

function Invoke-Step {
    param(
        [Parameter(Mandatory = $true)][string] $Name,
        [Parameter(Mandatory = $true)][scriptblock] $Action
    )
    Write-Host ""
    Write-Host "--- $Name" -ForegroundColor Cyan
    # Same PowerShell 5.1 quirk documented below for the compose config-sanity
    # calls: a native command's stderr line becomes a terminating
    # NativeCommandError under $ErrorActionPreference = "Stop", even at exit
    # code 0 - `uv lock --check` writes its "Resolved N packages" line to
    # stderr on every run, which was silently aborting this step before
    # $LASTEXITCODE was ever checked. Relaxed to Continue for exactly the
    # action's duration; $LASTEXITCODE is still checked explicitly below.
    $previousEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Action
    }
    finally {
        $ErrorActionPreference = $previousEap
    }
    if ($LASTEXITCODE -ne 0) {
        throw "FAIL: $Name (exit $LASTEXITCODE)"
    }
}

try {
    Write-Host "Thought Capture AI - repository checks"

    $Uv = Resolve-Uv

    # -----------------------------------------------------------------------
    # Repository baseline
    # -----------------------------------------------------------------------
    $RequiredPaths = @(
        "AGENTS.md",
        "CLAUDE.md",
        ".claude/agents/architecture-researcher.md",
        ".codex/agents/implementation-reviewer.toml",
        ".codex/agents/security-privacy-reviewer.toml",
        "README.md",
        "LICENSE",
        "docs/DESIGN.md",
        "docs/Thought-Capture-AI-System-Design.docx",
        "scripts/check.ps1",
        "scripts/check.sh",
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "env.example",
        "deploy/compose/docker-compose.yml",
        "alembic.ini",
        "migrations/env.py"
    )

    foreach ($Path in $RequiredPaths) {
        if (-not (Test-Path -LiteralPath $Path)) {
            throw "Required repository file is missing: $Path"
        }
    }
    Write-Host "OK: required files present"

    & git diff --check
    if ($LASTEXITCODE -ne 0) { throw "git diff --check failed" }
    Write-Host "OK: git diff --check"

    # A committed .env would leak credentials. Fail loudly rather than warn.
    $TrackedEnv = & git ls-files ".env" ".env.*"
    if ($TrackedEnv) {
        throw "Refusing to pass: environment file(s) are tracked by git: $TrackedEnv"
    }
    Write-Host "OK: no tracked .env files"

    # -----------------------------------------------------------------------
    # Toolchain, lint, types, tests
    # -----------------------------------------------------------------------
    Invoke-Step "lockfile is current" { & $Uv lock --check }
    Invoke-Step "format (ruff)"       { & $Uv run ruff format --check . }
    Invoke-Step "lint (ruff)"         { & $Uv run ruff check . }
    Invoke-Step "types (mypy)"        { & $Uv run mypy }
    Invoke-Step "unit tests"          { & $Uv run pytest -m "not integration and not contract" }

    # -----------------------------------------------------------------------
    # apps/embedding_sidecar carries its own toolchain (Python 3.12, own
    # pyproject.toml/uv.lock - docs/adr/0010 §5) and is deliberately excluded
    # from the root uv workspace, so none of the five steps above ever touch
    # it. Required, not skippable: this needs only uv + network access to
    # provision Python 3.12 (the same way the root project's own 3.14 is
    # provisioned), not Docker - a ruff/mypy/test regression here must fail
    # the same way a root regression does, not silently pass because nothing
    # ever ran it (CLAUDE.md: "extend both check scripts" whenever a
    # formatter/linter/type checker/test suite is added).
    # -----------------------------------------------------------------------
    Invoke-Step "embedding sidecar: lockfile is current" {
        Push-Location apps/embedding_sidecar
        try { & $Uv lock --check } finally { Pop-Location }
    }
    Invoke-Step "embedding sidecar: format (ruff)" {
        Push-Location apps/embedding_sidecar
        try { & $Uv run ruff format --check . } finally { Pop-Location }
    }
    Invoke-Step "embedding sidecar: lint (ruff)" {
        Push-Location apps/embedding_sidecar
        try { & $Uv run ruff check . } finally { Pop-Location }
    }
    Invoke-Step "embedding sidecar: types (mypy)" {
        Push-Location apps/embedding_sidecar
        try { & $Uv run mypy } finally { Pop-Location }
    }
    Invoke-Step "embedding sidecar: unit tests" {
        Push-Location apps/embedding_sidecar
        try { & $Uv run pytest } finally { Pop-Location }
    }

    # -----------------------------------------------------------------------
    # Integration tests: require PostgreSQL from the 'core' compose profile.
    # -----------------------------------------------------------------------
    Write-Host ""
    Write-Host "--- integration tests" -ForegroundColor Cyan
    $DockerUp = $false
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        & docker info --format "{{.ServerVersion}}" | Out-Null
        if ($LASTEXITCODE -eq 0) { $DockerUp = $true }
    }

    if ($DockerUp) {
        # Tells the integration conftest that a skipped test is a failure. An
        # unreachable database must never be reported as a passing suite.
        $env:TC_REQUIRE_INTEGRATION = "1"
        try {
            & $Uv run pytest -m integration
            if ($LASTEXITCODE -ne 0) { throw "FAIL: integration tests (exit $LASTEXITCODE)" }
        }
        finally {
            Remove-Item Env:\TC_REQUIRE_INTEGRATION -ErrorAction SilentlyContinue
        }
        Write-Host "OK: integration tests"
    }
    else {
        $Script:Skipped += "integration tests (Docker engine unavailable; start Docker Desktop, then: docker compose --env-file .env -f deploy/compose/docker-compose.yml --profile core up -d --build)"
        Write-Host "SKIPPED: Docker engine unavailable" -ForegroundColor Yellow
    }

    # -----------------------------------------------------------------------
    # Compose config sanity: the 'ai' profile's required TC_KHOJ_* variables
    # must never block a core-only deployment (review remediation, PR #17 -
    # Compose interpolates every service in every `-f` file before applying
    # `--profile` filtering, so a required Khoj variable living in the same
    # file as 'core' silently made 'ai' a hard dependency of 'core'). 'ai'
    # must still fail clearly, not silently, when its own secrets are absent.
    #
    # Uses a synthetic env file with only the 'core'-required variables,
    # deliberately not the real `.env` and deliberately not a process-level
    # override: `$env:VAR = ""` in PowerShell *deletes* the variable rather
    # than setting it empty, so it would fall straight through to whatever
    # a real `.env` already has - silently testing nothing. A from-scratch
    # env file has no such ambiguity and needs no real `.env` to exist.
    #
    # Client-side only (no running daemon needed for `config`), gated on
    # Docker being installed at all, matching the stages below.
    # -----------------------------------------------------------------------
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        Write-Host ""
        Write-Host "--- compose config sanity" -ForegroundColor Cyan
        $coreOnlyEnv = Join-Path ([System.IO.Path]::GetTempPath()) "tc-compose-sanity-core-only.env"
        $aiErrorFile = Join-Path ([System.IO.Path]::GetTempPath()) "tc-ai-compose-config-error.txt"
        "POSTGRES_PASSWORD=sanity-check-only`nTC_APP_DB_PASSWORD=sanity-check-only`n" |
            Set-Content -LiteralPath $coreOnlyEnv -Encoding utf8 -NoNewline
        # `docker compose ... config` writes routine warnings to stderr (e.g.
        # "TC_DISCORD_BOT_TOKEN not set, defaulting to blank string") on
        # every call. Under this script's `$ErrorActionPreference = "Stop"`,
        # PowerShell 5.1 wraps *any* redirected stderr line from a native
        # command into a NativeCommandError and - because of Stop - that
        # becomes a terminating exception the instant the first warning
        # line appears, aborting the script before $LASTEXITCODE is ever
        # checked. This is the same failure mode that made this script once
        # misreport `uv lock --check`. Redirection is still needed here (the
        # 'ai' case must inspect the *text* of the failure), so the fix is
        # to relax to Continue for exactly these two calls, not to avoid
        # redirection - `$LASTEXITCODE` is checked explicitly regardless.
        $previousEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & docker compose --env-file $coreOnlyEnv -f deploy/compose/docker-compose.yml --profile core config --quiet 2>$null
            if ($LASTEXITCODE -ne 0) {
                throw "FAIL: compose config sanity ('core' must validate without any TC_KHOJ_* set)"
            }
            & docker compose --env-file $coreOnlyEnv -f deploy/compose/docker-compose.yml -f deploy/compose/khoj.docker-compose.yml --profile ai config --quiet 2>$aiErrorFile
            if ($LASTEXITCODE -eq 0) {
                throw "FAIL: compose config sanity (expected 'ai' config to fail without TC_KHOJ_* set, but it succeeded)"
            }
            $aiError = Get-Content -Raw -LiteralPath $aiErrorFile -ErrorAction SilentlyContinue
            if ($aiError -notmatch "TC_KHOJ_") {
                throw "FAIL: compose config sanity ('ai' failed, but not for a missing TC_KHOJ_* variable: $aiError)"
            }

            # LLM configuration forwarding (independent Codex review): the
            # bot/worker environment must actually forward
            # TC_OPENROUTER_API_KEY and the model pins into the rendered
            # container environment, not just accept them at the top-level
            # interpolation stage - a variable can validate here and still
            # fail to reach a service if it were only referenced in, say,
            # `x-service-image` rather than `llm-env`. Just as important:
            # api/migrate must NOT receive them - `llm-env` is a separate
            # anchor from `service-env` specifically so a secret
            # (TC_OPENROUTER_API_KEY) and the model pins reach only the two
            # services with a code path that reads them (discord-bot's
            # select, worker's organize), not every container in the shared
            # network.
            $llmEnv = Join-Path ([System.IO.Path]::GetTempPath()) "tc-compose-sanity-llm.env"
            "POSTGRES_PASSWORD=sanity-check-only`nTC_APP_DB_PASSWORD=sanity-check-only`nTC_OPENROUTER_API_KEY=sanity-check-api-key`nTC_MODEL_ORGANIZE=sanity-check-organize-model`nTC_MODEL_SELECT=sanity-check-select-model`n" |
                Set-Content -LiteralPath $llmEnv -Encoding utf8 -NoNewline
            try {
                $llmConfig = & docker compose --env-file $llmEnv -f deploy/compose/docker-compose.yml --profile core config 2>$null
                if ($LASTEXITCODE -ne 0) {
                    throw "FAIL: compose config sanity (rendering 'core' config with LLM variables set failed)"
                }
                $llmConfigText = $llmConfig -join "`n"
                $lines = $llmConfigText -split "`n"

                function Get-ServiceBlock([string[]] $Lines, [string] $Service) {
                    $startIndex = [array]::IndexOf($Lines, "  ${Service}:")
                    if ($startIndex -lt 0) {
                        throw "FAIL: compose config sanity (service '$Service' not found in rendered config)"
                    }
                    $endIndex = $Lines.Length - 1
                    for ($i = $startIndex + 1; $i -lt $Lines.Length; $i++) {
                        if ($Lines[$i] -match "^  [a-zA-Z]") { $endIndex = $i - 1; break }
                    }
                    return ($Lines[$startIndex..$endIndex]) -join "`n"
                }

                foreach ($service in @("discord-bot", "worker")) {
                    $serviceBlock = Get-ServiceBlock $lines $service
                    if ($serviceBlock -notmatch "TC_OPENROUTER_API_KEY: sanity-check-api-key") {
                        throw "FAIL: compose config sanity ($service did not receive TC_OPENROUTER_API_KEY)"
                    }
                    if ($serviceBlock -notmatch "TC_MODEL_ORGANIZE: sanity-check-organize-model") {
                        throw "FAIL: compose config sanity ($service did not receive TC_MODEL_ORGANIZE)"
                    }
                    if ($serviceBlock -notmatch "TC_MODEL_SELECT: sanity-check-select-model") {
                        throw "FAIL: compose config sanity ($service did not receive TC_MODEL_SELECT)"
                    }
                }
                foreach ($service in @("api", "migrate")) {
                    $serviceBlock = Get-ServiceBlock $lines $service
                    if ($serviceBlock -match "TC_OPENROUTER_API_KEY") {
                        throw "FAIL: compose config sanity ($service must not receive TC_OPENROUTER_API_KEY, but does)"
                    }
                    if ($serviceBlock -match "TC_MODEL_(ORGANIZE|SELECT|QUERY_PLAN)") {
                        throw "FAIL: compose config sanity ($service must not receive model pins, but does)"
                    }
                }
            }
            finally {
                Remove-Item -LiteralPath $llmEnv -ErrorAction SilentlyContinue
            }
        }
        finally {
            $ErrorActionPreference = $previousEap
            Remove-Item -LiteralPath $coreOnlyEnv -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $aiErrorFile -ErrorAction SilentlyContinue
        }
        Write-Host "OK: compose config sanity"
    }
    else {
        $Script:Skipped += "compose config sanity (Docker engine unavailable)"
        Write-Host ""
        Write-Host "SKIPPED: compose config sanity (Docker engine unavailable)" -ForegroundColor Yellow
    }

    # -----------------------------------------------------------------------
    # Contract tests: require the pinned Khoj instance from the 'ai' compose
    # profile (docs/adr/0003). Unlike PostgreSQL/'core' above, 'ai' is a new,
    # heavy, optional-so-far dependency - reachability is probed explicitly
    # (Docker being up does not imply this profile was ever started) and an
    # unreachable Khoj is a skip, not a hard failure, until Phase 2 makes it
    # required.
    # -----------------------------------------------------------------------
    Write-Host ""
    Write-Host "--- contract tests (khoj)" -ForegroundColor Cyan
    $KhojUrl = if ($env:TC_KHOJ_BASE_URL) { $env:TC_KHOJ_BASE_URL } else { "http://127.0.0.1:42110" }
    $KhojUp = $false
    try {
        $response = Invoke-WebRequest -Uri "$KhojUrl/api/search?q=check" -TimeoutSec 3 -UseBasicParsing
        if ($response.StatusCode -eq 200) { $KhojUp = $true }
    }
    catch {
        $KhojUp = $false
    }

    if ($KhojUp) {
        $env:TC_REQUIRE_CONTRACT = "1"
        try {
            & $Uv run pytest -m contract tests/contract/khoj
            if ($LASTEXITCODE -ne 0) { throw "FAIL: contract tests (khoj) (exit $LASTEXITCODE)" }
        }
        finally {
            Remove-Item Env:\TC_REQUIRE_CONTRACT -ErrorAction SilentlyContinue
        }
        Write-Host "OK: contract tests (khoj)"
    }
    else {
        $Script:Skipped += "contract tests (khoj) (Khoj unreachable at $KhojUrl; docker compose --env-file .env -f deploy/compose/docker-compose.yml -f deploy/compose/khoj.docker-compose.yml --profile ai up -d)"
        Write-Host "SKIPPED: Khoj unreachable at $KhojUrl" -ForegroundColor Yellow
    }

    # -------------------------------------------------------------------
    # Contract tests: require the embedding sidecar from the 'core' compose
    # profile (docs/adr/0010). First-party and part of 'core', not an
    # optional add-on the way Khoj/'ai' is - still gated on explicit
    # reachability, not a hard failure, since 'core' being started at all is
    # not implied by Docker merely being available.
    # -------------------------------------------------------------------
    Write-Host ""
    Write-Host "--- contract tests (embedding sidecar)" -ForegroundColor Cyan
    $EmbeddingSidecarUrl = if ($env:TC_EMBEDDING_SIDECAR_BASE_URL) { $env:TC_EMBEDDING_SIDECAR_BASE_URL } else { "http://127.0.0.1:8081" }
    $EmbeddingSidecarUp = $false
    try {
        $response = Invoke-WebRequest -Uri "$EmbeddingSidecarUrl/health" -TimeoutSec 3 -UseBasicParsing
        if ($response.StatusCode -eq 200) { $EmbeddingSidecarUp = $true }
    }
    catch {
        $EmbeddingSidecarUp = $false
    }

    if ($EmbeddingSidecarUp) {
        $env:TC_REQUIRE_CONTRACT = "1"
        try {
            & $Uv run pytest -m contract tests/contract/embedding_sidecar
            if ($LASTEXITCODE -ne 0) { throw "FAIL: contract tests (embedding sidecar) (exit $LASTEXITCODE)" }
        }
        finally {
            Remove-Item Env:\TC_REQUIRE_CONTRACT -ErrorAction SilentlyContinue
        }
        Write-Host "OK: contract tests (embedding sidecar)"
    }
    else {
        $Script:Skipped += "contract tests (embedding sidecar) (unreachable at $EmbeddingSidecarUrl; docker compose --env-file .env -f deploy/compose/docker-compose.yml --profile core up -d --build embedding-sidecar)"
        Write-Host "SKIPPED: embedding sidecar unreachable at $EmbeddingSidecarUrl" -ForegroundColor Yellow
    }

    # -----------------------------------------------------------------------
    Write-Host ""
    if ($Script:Skipped.Count -gt 0) {
        Write-Host "PASS WITH SKIPS" -ForegroundColor Yellow
        foreach ($item in $Script:Skipped) {
            Write-Host "  NOT RUN: $item" -ForegroundColor Yellow
        }
        Write-Host "These checks were not executed. Do not treat this run as full verification."
    }
    else {
        Write-Host "PASS: all checks" -ForegroundColor Green
    }

    # Explicit success. Probing commands above (e.g. `docker info`) leave a
    # non-zero $LASTEXITCODE behind, which would otherwise become this script's
    # exit code and make a passing run look like a failure in CI.
    exit 0
}
catch {
    Write-Host ""
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
finally {
    Pop-Location
}
