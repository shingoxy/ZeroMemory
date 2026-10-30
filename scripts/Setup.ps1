#Requires -Version 5.1
<#
.SYNOPSIS
Install the locked project environment without changing global Python packages.
#>
[CmdletBinding()]
param([ValidateNotNullOrEmpty()][string]$Python = '3.14')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'Install uv first: https://docs.astral.sh/uv/' }
Push-Location -LiteralPath $projectRoot
try {
    New-Item -ItemType Directory -Force -Path '.cache/tmp' | Out-Null
    $env:UV_CACHE_DIR = Join-Path $projectRoot '.cache/uv'
    $env:UV_LINK_MODE = 'copy'
    $env:TEMP = Join-Path $projectRoot '.cache/tmp'
    $env:TMP = $env:TEMP
    & uv sync --frozen --python $Python
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
    Write-Output 'Ready. Run .\scripts\Start-ZeroMemory.ps1'
} finally { Pop-Location }
