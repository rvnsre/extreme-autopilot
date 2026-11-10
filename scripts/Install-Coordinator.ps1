param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed' }
    & .\.venv\Scripts\python.exe -m pip install --requirement requirements-economy.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    & .\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
    if ($LASTEXITCODE -ne 0) { throw 'Package installation failed' }
    Push-Location web
    try {
        & npm ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw 'Panel dependency installation failed' }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Panel build failed' }
    } finally { Pop-Location }
    Write-Host 'Installed. Follow docs/SETUP.md to configure credentials, models and VM TLS.'
} finally { Pop-Location }
