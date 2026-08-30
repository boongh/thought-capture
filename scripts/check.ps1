$ErrorActionPreference = "Stop"

$RepositoryRoot = Split-Path -Parent $PSScriptRoot
Push-Location $RepositoryRoot

try {
    Write-Host "Thought Capture AI - repository checks"

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
        "scripts/check.sh"
    )

    foreach ($Path in $RequiredPaths) {
        if (-not (Test-Path -LiteralPath $Path)) {
            throw "Required repository file is missing: $Path"
        }
    }

    & git diff --check
    if ($LASTEXITCODE -ne 0) {
        throw "git diff --check failed"
    }

    Write-Host "PASS: design-stage repository baseline"
    Write-Host "NOTE: application checks are not configured yet."
    Write-Host "Future implementation must extend this script with formatting, linting, typing, migrations, tests, and service contract checks."
}
finally {
    Pop-Location
}
