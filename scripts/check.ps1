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
    & $Action
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
        $Script:Skipped += "integration tests (Docker engine unavailable; start Docker Desktop, then: docker compose --env-file .env -f deploy/compose/docker-compose.yml --profile core up -d)"
        Write-Host "SKIPPED: Docker engine unavailable" -ForegroundColor Yellow
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
    Write-Host "--- contract tests" -ForegroundColor Cyan
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
            & $Uv run pytest -m contract
            if ($LASTEXITCODE -ne 0) { throw "FAIL: contract tests (exit $LASTEXITCODE)" }
        }
        finally {
            Remove-Item Env:\TC_REQUIRE_CONTRACT -ErrorAction SilentlyContinue
        }
        Write-Host "OK: contract tests"
    }
    else {
        $Script:Skipped += "contract tests (Khoj unreachable at $KhojUrl; docker compose --env-file .env -f deploy/compose/docker-compose.yml --profile ai up -d)"
        Write-Host "SKIPPED: Khoj unreachable at $KhojUrl" -ForegroundColor Yellow
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
