param([Parameter(Mandatory=$true)][string]$Manifest, [switch]$Apply, [switch]$Supervise)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not [IO.Path]::IsPathRooted($Manifest)) { throw 'absolute_manifest_required' }
$Action = if ($Supervise -and $Apply) { 'run' } else { 'start' }
$Arguments = @('-B', (Join-Path $PSScriptRoot 'supervisor.py'), $Action, '--manifest', $Manifest)
if ($Apply -and -not $Supervise) { $Arguments += '--apply' }
& $PythonExe @Arguments
exit $LASTEXITCODE
