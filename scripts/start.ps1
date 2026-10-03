param([int]$Port = 8765, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot '.runtime'
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$LauncherPath = Join-Path $PSScriptRoot 'run_server.py'
$StatePath = Join-Path $RuntimeDir 'processes.json'
$StopPath = Join-Path $RuntimeDir 'stop.request'
if ($Port -lt 1024 -or $Port -gt 65535) { throw 'Port must be between 1024 and 65535.' }
if (-not (Test-Path -LiteralPath $PythonExe) -or -not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'frontend\dist\index.html'))) {
    & (Join-Path $PSScriptRoot 'bootstrap.ps1')
}
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
if (Test-Path -LiteralPath $StatePath) {
    $State = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    $StateCreatedStamp = if ($State.creation_time -is [datetime]) { $State.creation_time.ToUniversalTime().ToString('o') } else { [string]$State.creation_time }
    $Existing = Get-CimInstance Win32_Process -Filter "ProcessId=$($State.pid)" -ErrorAction SilentlyContinue
    if ($Existing -and $Existing.CommandLine -like "*$LauncherPath*" -and $Existing.CreationDate.ToUniversalTime().ToString('o') -eq $StateCreatedStamp) {
        Write-Output "Secretary is already running: http://127.0.0.1:$($State.port)"
        exit 0
    }
}
$Listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($Listener) { throw "Port $Port is occupied. No process was stopped. Choose another -Port." }
if (Test-Path -LiteralPath $StopPath) { Remove-Item -LiteralPath $StopPath }
$Arguments = '"' + $LauncherPath + '" --port ' + $Port
$Started = Start-Process -FilePath $PythonExe -ArgumentList $Arguments -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $RuntimeDir 'server.stdout.log') -RedirectStandardError (Join-Path $RuntimeDir 'server.stderr.log')
$Owned = Get-CimInstance Win32_Process -Filter "ProcessId=$($Started.Id)"
@{pid=$Started.Id;port=$Port;creation_time=$Owned.CreationDate.ToUniversalTime().ToString('o');launcher=$LauncherPath;project=$ProjectRoot} | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
$Ready = $false
for ($Attempt = 0; $Attempt -lt 60; $Attempt++) {
    if ($Started.HasExited) { throw "Secretary exited. Inspect $RuntimeDir\server.stderr.log." }
    try {
        $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 1
        if ($Health.status -eq 'ok') { $Ready = $true; break }
    } catch { }
    Start-Sleep -Milliseconds 500
}
if (-not $Ready) { throw 'Secretary did not become healthy within 30 seconds. Process remains tracked; run stop.ps1.' }
Write-Output "Secretary: http://127.0.0.1:$Port"
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port" }
