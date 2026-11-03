#Requires -Version 5.1
<#
.SYNOPSIS
Run lint, JavaScript syntax, all automated tests, and a real-process synthetic acceptance demo.
#>
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe'
$env:PYTHONUTF8 = '1'
Push-Location -LiteralPath $projectRoot
try {
    & $pythonExe -m ruff check src tests scripts
    if ($LASTEXITCODE -ne 0) { throw 'Lint failed.' }
    if (Get-Command node -ErrorAction SilentlyContinue) {
        & node --check web/app.js
        if ($LASTEXITCODE -ne 0) { throw 'JavaScript syntax failed.' }
        & node --check web/observatory.js
        if ($LASTEXITCODE -ne 0) { throw 'Observatory JavaScript syntax failed.' }
        & node --check web/activity.js
        if ($LASTEXITCODE -ne 0) { throw 'Activity JavaScript syntax failed.' }
        & node --check web/raw-history.js
        if ($LASTEXITCODE -ne 0) { throw 'Raw history JavaScript syntax failed.' }
        & node --check web/review.js
        if ($LASTEXITCODE -ne 0) { throw 'Review JavaScript syntax failed.' }
    }
    $testDirectory = Join-Path $projectRoot ('output/pytest-' + [guid]::NewGuid().ToString())
    & $pythonExe -m pytest -q --basetemp $testDirectory --junitxml output/junit.xml
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
    & $pythonExe scripts/acceptance_demo.py --output output
    if ($LASTEXITCODE -ne 0) { throw 'Acceptance demo failed.' }
} finally { Pop-Location }
