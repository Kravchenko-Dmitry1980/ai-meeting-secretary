$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot '.runtime'
$StatePath = Join-Path $RuntimeDir 'processes.json'
if (-not (Test-Path -LiteralPath $StatePath)) { Write-Output 'Secretary is not tracked as running.'; exit 0 }
$State = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
if ($State.owner_kind -eq 'local_supervisor') {
    & (Join-Path $ProjectRoot '.venv\Scripts\python.exe') -B (Join-Path $PSScriptRoot 'local_supervisor.py') stop --runtime-root $RuntimeDir
    if ($LASTEXITCODE -ne 0) { throw 'Supervised Secretary stop did not verify quiescence. Its ownership state was retained.' }
    exit 0
}
$OwnerLeasePath = Join-Path $RuntimeDir 'local-owner.lock'
try { $OwnerLease = [IO.File]::Open($OwnerLeasePath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
catch { throw 'Secretary local ownership is busy. No state was removed or process stopped.' }
try {
$State = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
if ($State.owner_kind -eq 'local_supervisor') { throw 'Secretary ownership changed. Run stop again; no state was removed.' }
$StateCreatedStamp = if ($State.creation_time -is [datetime]) { $State.creation_time.ToUniversalTime().ToString('o') } else { [string]$State.creation_time }
$ExpectedLauncher = Join-Path $PSScriptRoot 'run_server.py'
$Owned = Get-CimInstance Win32_Process -Filter "ProcessId=$($State.pid)" -ErrorAction SilentlyContinue
if (-not $Owned) { Remove-Item -LiteralPath $StatePath; Write-Output 'Secretary already stopped.'; exit 0 }
if ($Owned.CommandLine -notlike "*$ExpectedLauncher*" -or $Owned.CreationDate.ToUniversalTime().ToString('o') -ne $StateCreatedStamp -or $State.project -ne $ProjectRoot) {
    throw 'Process identity differs from this Secretary instance. No process was stopped.'
}
$StopName = 'stop.request'
if ($State.run_id) {
    if ([string]$State.run_id -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') { throw 'Secretary launch identity is invalid. No stop marker was written.' }
    $StopName = 'stop-' + [string]$State.run_id + '.request'
}
Set-Content -LiteralPath (Join-Path $RuntimeDir $StopName) -Value 'stop' -Encoding ASCII
for ($Attempt = 0; $Attempt -lt 60; $Attempt++) {
    if (-not (Get-Process -Id $State.pid -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $StatePath
        Write-Output 'Secretary stopped gracefully.'
        exit 0
    }
    Start-Sleep -Milliseconds 500
}
throw 'Graceful stop timed out. Process identity was verified but it was not force-killed; inspect Secretary logs.'
} finally { $OwnerLease.Dispose() }
