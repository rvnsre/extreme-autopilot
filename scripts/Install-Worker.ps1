param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
if ($env:EA_ISOLATED_WORKER -ne 'true') {
    throw 'Run only INSIDE the dedicated Windows VM, after setting EA_ISOLATED_WORKER=true.'
}
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed' }
    & .\.venv\Scripts\python.exe -m pip install --requirement requirements-worker.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Worker dependency installation failed' }
    & .\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
    if ($LASTEXITCODE -ne 0) { throw 'Package installation failed' }
    & .\.venv\Scripts\python.exe -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw 'Chromium installation failed' }
    Write-Host 'Installed. Configure the worker-only env file and run autopilot worker in its interactive session.'
} finally { Pop-Location }
