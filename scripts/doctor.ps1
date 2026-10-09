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
        $BaseUri = "http://127.0.0.1:$($State.port)"
        try {
            $Health = Invoke-RestMethod -Uri "$BaseUri/health" -TimeoutSec 2
            Write-Output "Health (liveness): $($Health.status)"
        } catch { Write-Output 'Health (liveness): unreachable' }
        try {
            $Readiness = Invoke-RestMethod -Uri "$BaseUri/ready" -TimeoutSec 8
            Write-Output "Local readiness: $($Readiness.status)"
            Write-Output "Production qualification: $($Readiness.production_qualified)"
            foreach ($Check in @(
                @{Key='database';Label='SQLite'}, @{Key='storage';Label='Data storage'},
                @{Key='ffmpeg';Label='FFmpeg'}, @{Key='ffprobe';Label='FFprobe'},
                @{Key='frontend_build';Label='Frontend build'},
                @{Key='release_parity';Label='Release versions'},
                @{Key='processing_worker';Label='Processing worker'},
                @{Key='publication_worker';Label='Publication worker'},
                @{Key='cloud';Label='Cloud'}, @{Key='device_capture';Label='Audio device'},
                @{Key='maintenance';Label='Maintenance'}, @{Key='backup';Label='Backup age'},
                @{Key='logging';Label='Log rotation'}
            )) {
                $ComponentProperty = $Readiness.components.PSObject.Properties[$Check.Key]
                if ($ComponentProperty) {
                    $Component = $ComponentProperty.Value
                    $Detail = ''
                    if ($Check.Key -eq 'storage' -and $null -ne $Component.free_bytes) {
                        $FreeGiB = [math]::Round(([double]$Component.free_bytes / 1GB), 2)
                        $Detail = "; writable=$($Component.directory_writable); free=${FreeGiB} GiB"
                    } elseif ($Check.Key -eq 'processing_worker') {
                        $Detail = "; heartbeat_age_seconds=$($Component.heartbeat_age_seconds)"
                    } elseif ($Check.Key -eq 'release_parity') {
                        if ($Component.backend_version -or $Component.frontend_version) {
                            $Detail = "; backend=$($Component.backend_version); frontend=$($Component.frontend_version)"
                        }
                        if ($null -ne $Component.lockfiles_verified) {
                            $Detail += "; lockfiles_verified=$($Component.lockfiles_verified)"
                        }
                        if ($Component.build_check) { $Detail += "; build_check=$($Component.build_check)" }
                        if ($Component.code) { $Detail += "; code=$($Component.code)" }
                    }
                    Write-Output "$($Check.Label): $($Component.state)$Detail"
                }
            }
            $Counts = $Readiness.jobs.counts | ConvertTo-Json -Compress
            Write-Output "Jobs by status (no meeting identifiers): $Counts; stale running candidates=$($Readiness.jobs.stale_running_candidate_count)"
            if ($Readiness.blockers.Count -gt 0) { Write-Output "Readiness blockers: $($Readiness.blockers -join ', ')" }
            try {
                $Session = Invoke-RestMethod -Uri "$BaseUri/api/v1/session" -TimeoutSec 3
                $StorageProbe = Invoke-RestMethod -Uri "$BaseUri/ready/storage-probe" -Method Post `
                    -Headers @{ 'X-Secretary-Token' = [string]$Session.csrf_token } -TimeoutSec 8
                Write-Output "Data write probe: $($StorageProbe.state)"
                if ($StorageProbe.code) { Write-Output "Data write probe code: $($StorageProbe.code)" }
            } catch {
                Write-Output 'Data write probe: unavailable or failed (no raw error details recorded)'
            }
        } catch {
            Write-Output 'Local readiness: unavailable (this running process may need a planned restart to load the new diagnostics endpoint)'
        }
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
