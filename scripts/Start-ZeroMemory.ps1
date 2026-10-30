#Requires -Version 5.1
<#
.SYNOPSIS
Start the local ZeroMemory service in the current terminal. Ctrl+C stops it.
#>
[CmdletBinding()]
param([string]$DataHome, [ValidateRange(1024, 65535)][int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Run .\scripts\Setup.ps1 first.' }
$taskArguments = @('-m', 'zero_memory', '--port', $Port.ToString())
if ($DataHome) { $taskArguments += @('--home', $DataHome) }
$env:PYTHONUTF8 = '1'
$env:ZERO_HOST = '127.0.0.1'
Push-Location -LiteralPath $projectRoot
try { & $pythonExe @taskArguments } finally { Pop-Location }
