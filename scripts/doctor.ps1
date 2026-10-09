$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
Write-Output "Project: $ProjectRoot"
foreach ($CommandName in @('uv','node','npm.cmd','ffmpeg','ffprobe')) {
    $Found = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($Found) { Write-Output "$CommandName : $($Found.Source)" } else { Write-Output "$CommandName : MISSING" }
}
if (Test-Path -LiteralPath $PythonExe) {
    $PythonInfoCode = @'
import sys, importlib.metadata as m
print("Python:", sys.version.split()[0])
print("Interpreter:", sys.executable)
print("FastAPI:", m.version("fastapi"))
print("PyAudioWPatch:", m.version("PyAudioWPatch"))
'@
    $PythonInfoCode | & $PythonExe -
} else { Write-Output 'Virtual environment: MISSING' }
Write-Output "Frontend build: $(Test-Path -LiteralPath (Join-Path $ProjectRoot 'frontend\dist\index.html'))"
Write-Output "Local .env present: $(Test-Path -LiteralPath (Join-Path $ProjectRoot '.env'))"
Write-Output "Python lock: $(Test-Path -LiteralPath (Join-Path $ProjectRoot 'uv.lock'))"
Write-Output "Frontend lock: $(Test-Path -LiteralPath (Join-Path $ProjectRoot 'frontend\package-lock.json'))"
$StatePath = Join-Path $ProjectRoot '.runtime\processes.json'
if (Test-Path -LiteralPath $StatePath) {
    $State = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    $LauncherPath = Join-Path $PSScriptRoot 'run_server.py'
    $TrackedPid = [long]0
    $PidParsed = [long]::TryParse([string]$State.pid, [ref]$TrackedPid)
    $TrackedProcess = if ($PidParsed -and $TrackedPid -gt 0 -and $TrackedPid -le [int]::MaxValue) {
        Get-CimInstance Win32_Process -Filter "ProcessId=$TrackedPid" -ErrorAction SilentlyContinue
    }
    $StateCreatedUtc = [datetime]::MinValue
    $CreatedStampParsed = [datetime]::TryParse(
        [string]$State.creation_time,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal,
        [ref]$StateCreatedUtc
    )
    $CreationMatches = $false
    if ($TrackedProcess -and $CreatedStampParsed) {
        $CreationDelta = [math]::Abs(($TrackedProcess.CreationDate.ToUniversalTime() - $StateCreatedUtc.ToUniversalTime()).TotalSeconds)
        $CreationMatches = $CreationDelta -le 2
    }
    $IdentityMatches = $TrackedProcess -and $CreationMatches -and
        $TrackedProcess.CommandLine -like "*$LauncherPath*" -and
        $TrackedProcess.CommandLine -like "*--run-id $($State.run_id)*"
    if ($IdentityMatches) {
        Write-Output 'Tracked process identity: verified'
        if ($State.offline -eq $true) {
            Write-Output 'Outbound guard: disabled by offline launch'
        } else {
            Write-Output 'Outbound guard: not forced off by launcher; check application settings'
        }
        if ([string]::IsNullOrWhiteSpace([string]$State.team_runtime_config)) {
            Write-Output 'Team runtime: not configured'
        } else {
            Write-Output 'Team runtime: configured (path hidden)'
        }
        try { $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$($State.port)/health" -TimeoutSec 2; Write-Output "Health: $($Health.status)" } catch { Write-Output 'Health: unreachable' }
    } else {
        if (-not $TrackedProcess) {
            Write-Output 'Tracked process: stale (recorded PID is not running)'
            $Listener = Get-NetTCPConnection -LocalPort $State.port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($Listener) {
                Write-Output 'Listener: present, but process identity is not linked to the saved launch record'
            } else {
                Write-Output 'Listener: not found'
            }
        } else {
            Write-Output 'Tracked process identity: not verified'
        }
        Write-Output 'Health: not queried for an unverified process'
    }
} else {
    Write-Output 'Tracked process: not found'
    Write-Output 'Health: not queried'
}
Write-Output 'No secrets displayed. Device capture and cloud quality need separate verification.'
