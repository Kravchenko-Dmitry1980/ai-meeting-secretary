[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Backup,
    [Parameter(Mandatory=$true)][string]$Destination,
    [Parameter(Mandatory=$true)][string]$RestoreRoot,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$scriptPath = Join-Path $PSScriptRoot 'backup_restore.py'
$arguments = @('-B', $scriptPath, 'restore', '--backup', $Backup, '--destination', $Destination, '--restore-root', $RestoreRoot)
if ($Apply) { $arguments += '--apply' }
& $pythonPath @arguments
exit $LASTEXITCODE
