param([Parameter(Mandatory=$true)][string]$Manifest, [switch]$ReadOnly)
$ErrorActionPreference = 'Stop'
if (-not $ReadOnly) { throw 'read_only_required' }
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not [IO.Path]::IsPathRooted($Manifest)) { throw 'absolute_manifest_required' }
& (Join-Path $ProjectRoot '.venv\Scripts\python.exe') -B (Join-Path $PSScriptRoot 'supervisor.py') doctor --manifest $Manifest
exit $LASTEXITCODE
