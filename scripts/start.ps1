param([int]$Port = 8765, [switch]$NoBrowser, [switch]$NoBootstrap, [switch]$Offline, [string]$TeamRuntimeConfig)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot '.runtime'
$LogDir = Join-Path $RuntimeDir 'logs'
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$LauncherPath = Join-Path $PSScriptRoot 'run_server.py'
$StatePath = Join-Path $RuntimeDir 'processes.json'
$StopPath = Join-Path $RuntimeDir 'stop.request'
if ($Port -lt 1024 -or $Port -gt 65535) { throw 'Port must be between 1024 and 65535.' }
if ($Offline -and $TeamRuntimeConfig) { throw 'Offline mode cannot be combined with TeamRuntimeConfig.' }
if ($TeamRuntimeConfig) {
    if (-not [IO.Path]::IsPathRooted($TeamRuntimeConfig) -or -not (Test-Path -LiteralPath $TeamRuntimeConfig -PathType Leaf)) { throw 'TeamRuntimeConfig must identify an existing absolute configuration path.' }
    $TeamRuntimeConfig = (Resolve-Path -LiteralPath $TeamRuntimeConfig).Path
}
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
if (Test-Path -LiteralPath $StatePath) {
    $State = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    $StateCreatedStamp = if ($State.creation_time -is [datetime]) { $State.creation_time.ToUniversalTime().ToString('o') } else { [string]$State.creation_time }
    $Existing = Get-CimInstance Win32_Process -Filter "ProcessId=$($State.pid)" -ErrorAction SilentlyContinue
    if ($Existing -and $Existing.CommandLine -like "*$LauncherPath*" -and $Existing.CreationDate.ToUniversalTime().ToString('o') -eq $StateCreatedStamp) {
        $ExistingOffline = $State.offline -eq $true
        if ($ExistingOffline -ne [bool]$Offline) { throw 'An existing Secretary process has a different outbound mode. No process was adopted or stopped.' }
        if ($TeamRuntimeConfig -and $State.team_runtime_config -ne $TeamRuntimeConfig) { throw 'An existing Secretary process has a different Team configuration. No process was adopted or stopped.' }
        Write-Output "Secretary is already running: http://127.0.0.1:$($State.port)"
        exit 0
    }
}
$ActiveRuns = @(
    Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.CommandLine -and $_.CommandLine.IndexOf($LauncherPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
    } | ForEach-Object {
        $CommandLine = [string]$_.CommandLine
        $ProcessId = [int]$_.ProcessId
        $PortMatch = [regex]::Match($CommandLine, '(?:^|\s)--port(?:=|\s+)(?<value>\d{1,5})(?:\s|$)')
        $RunIdMatch = [regex]::Match($CommandLine, '(?:^|\s)--run-id(?:=|\s+)(?<value>[0-9a-fA-F-]{36})(?:\s|$)')
        $ProcessPort = if ($PortMatch.Success) { [int]$PortMatch.Groups['value'].Value } else { 0 }
        $OwnsListener = $false
        if ($ProcessPort -gt 0) {
            $OwnsListener = [bool](Get-NetTCPConnection -LocalPort $ProcessPort -State Listen -ErrorAction SilentlyContinue |
                Where-Object { $_.OwningProcess -eq $ProcessId } | Select-Object -First 1)
        }
        [pscustomobject]@{
            Pid = $ProcessId
            Port = $ProcessPort
            RunId = if ($RunIdMatch.Success) { $RunIdMatch.Groups['value'].Value } else { 'unknown' }
            Listening = $OwnsListener
        }
    }
)
if ($ActiveRuns.Count -gt 0) {
    $ActiveRunDetails = @($ActiveRuns | Group-Object {
        if ($_.RunId -ne 'unknown') { "$($_.Port):$($_.RunId)" } else { "pid:$($_.Pid)" }
    } | ForEach-Object {
        $Group = $_.Group
        $Representative = $Group | Where-Object { $_.Listening } | Select-Object -First 1
        if (-not $Representative) { $Representative = $Group | Select-Object -First 1 }
        $PortLabel = if ($Representative.Port -gt 0) { [string]$Representative.Port } else { 'unknown' }
        "PID $($Representative.Pid), port $PortLabel, run $($Representative.RunId)"
    }) -join '; '
    throw "A Secretary process from this checkout is already active ($ActiveRunDetails). No process was stopped or adopted."
}
$Listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($Listener) { throw "Port $Port is occupied. No process was stopped. Choose another -Port." }
if (-not (Test-Path -LiteralPath $PythonExe) -or -not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'frontend\dist\index.html'))) {
    if ($NoBootstrap) { throw 'Secretary runtime is not prepared. Run bootstrap.ps1 explicitly before enabling the scheduled launcher.' }
    & (Join-Path $PSScriptRoot 'bootstrap.ps1')
}
$LaunchId = [guid]::NewGuid().ToString()
$StopPath = Join-Path $RuntimeDir ('stop-' + $LaunchId + '.request')
if (Test-Path -LiteralPath $StopPath) { Remove-Item -LiteralPath $StopPath }
$StdoutLog = Join-Path $RuntimeDir ('server-' + $LaunchId + '.stdout.log')
$StderrLog = Join-Path $RuntimeDir ('server-' + $LaunchId + '.stderr.log')
$Arguments = '"' + $LauncherPath + '" --port ' + $Port + ' --run-id ' + $LaunchId + ' --log-directory "' + $LogDir + '"'
if ($TeamRuntimeConfig) {
    $Arguments += ' --team-runtime-config "' + $TeamRuntimeConfig + '"'
}
if ($Offline) { $Arguments += ' --offline' }
$Started = Start-Process -FilePath $PythonExe -ArgumentList $Arguments -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog
$Owned = Get-CimInstance Win32_Process -Filter "ProcessId=$($Started.Id)"
@{pid=$Started.Id;port=$Port;creation_time=$Owned.CreationDate.ToUniversalTime().ToString('o');launcher=$LauncherPath;project=$ProjectRoot;run_id=$LaunchId;team_runtime_config=$TeamRuntimeConfig;offline=[bool]$Offline;stdout_log=$StdoutLog;stderr_log=$StderrLog} | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
$Ready = $false
for ($Attempt = 0; $Attempt -lt 60; $Attempt++) {
    if ($Started.HasExited) { throw "Secretary exited. Inspect $StderrLog." }
    try {
        $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 1
        if ($Health.status -eq 'ok') { $Ready = $true; break }
    } catch { }
    Start-Sleep -Milliseconds 500
}
if (-not $Ready) { throw 'Secretary did not become healthy within 30 seconds. Process remains tracked; run stop.ps1.' }
Write-Output "Secretary: http://127.0.0.1:$Port"
if ($Offline) { Write-Output 'Outbound integrations are disabled for this process.' }
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port" }
