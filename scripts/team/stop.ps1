param([Parameter(Mandatory=$true)][string]$Manifest, [switch]$Apply)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not [IO.Path]::IsPathRooted($Manifest)) { throw 'absolute_manifest_required' }
$Arguments = @('-B', (Join-Path $PSScriptRoot 'supervisor.py'), 'stop', '--manifest', $Manifest)
if ($Apply) { $Arguments += '--apply' }
& $PythonExe @Arguments
exit $LASTEXITCODE
