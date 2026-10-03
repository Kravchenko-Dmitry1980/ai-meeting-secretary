$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot '.runtime'
$StatePath = Join-Path $RuntimeDir 'processes.json'
if (-not (Test-Path -LiteralPath $StatePath)) { Write-Output 'Secretary is not tracked as running.'; exit 0 }
$State = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
$StateCreatedStamp = if ($State.creation_time -is [datetime]) { $State.creation_time.ToUniversalTime().ToString('o') } else { [string]$State.creation_time }
$ExpectedLauncher = Join-Path $PSScriptRoot 'run_server.py'
$Owned = Get-CimInstance Win32_Process -Filter "ProcessId=$($State.pid)" -ErrorAction SilentlyContinue
if (-not $Owned) { Remove-Item -LiteralPath $StatePath; Write-Output 'Secretary already stopped.'; exit 0 }
if ($Owned.CommandLine -notlike "*$ExpectedLauncher*" -or $Owned.CreationDate.ToUniversalTime().ToString('o') -ne $StateCreatedStamp -or $State.project -ne $ProjectRoot) {
    throw 'Process identity differs from this Secretary instance. No process was stopped.'
}
Set-Content -LiteralPath (Join-Path $RuntimeDir 'stop.request') -Value 'stop' -Encoding ASCII
for ($Attempt = 0; $Attempt -lt 60; $Attempt++) {
    if (-not (Get-Process -Id $State.pid -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $StatePath
        Write-Output 'Secretary stopped gracefully.'
        exit 0
    }
    Start-Sleep -Milliseconds 500
}
throw 'Graceful stop timed out. Process identity was verified but it was not force-killed; inspect Secretary logs.'
