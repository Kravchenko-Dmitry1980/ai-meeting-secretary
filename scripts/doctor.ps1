$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
Write-Output "Project: $ProjectRoot"
foreach ($CommandName in @('uv','node','npm.cmd','ffmpeg','ffprobe')) {
    $Found = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($Found) { Write-Output "$CommandName : $($Found.Source)" } else { Write-Output "$CommandName : MISSING" }
}
if (Test-Path -LiteralPath $PythonExe) {
    & $PythonExe -c 'import sys, importlib.metadata as m; print("Python:", sys.version.split()[0]); print("Interpreter:", sys.executable); print("FastAPI:", m.version("fastapi")); print("PyAudioWPatch:", m.version("PyAudioWPatch"))'
} else { Write-Output 'Virtual environment: MISSING' }
Write-Output "Frontend build: $(Test-Path -LiteralPath (Join-Path $ProjectRoot 'frontend\dist\index.html'))"
Write-Output "Local .env present: $(Test-Path -LiteralPath (Join-Path $ProjectRoot '.env'))"
Write-Output "Python lock: $(Test-Path -LiteralPath (Join-Path $ProjectRoot 'uv.lock'))"
Write-Output "Frontend lock: $(Test-Path -LiteralPath (Join-Path $ProjectRoot 'frontend\package-lock.json'))"
$StatePath = Join-Path $ProjectRoot '.runtime\processes.json'
if (Test-Path -LiteralPath $StatePath) {
    $State = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    try { $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$($State.port)/health" -TimeoutSec 2; Write-Output "Health: $($Health.status)" } catch { Write-Output 'Health: unreachable' }
}
Write-Output 'No secrets displayed. Device capture and cloud quality need separate verification.'
