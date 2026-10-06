[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Config,
    [Parameter(Mandatory=$true)][string]$Destination,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$scriptPath = Join-Path $PSScriptRoot 'backup_restore.py'
$arguments = @('-B', $scriptPath, 'backup', '--config', $Config, '--destination', $Destination)
if ($Apply) { $arguments += '--apply' }
& $pythonPath @arguments
exit $LASTEXITCODE
