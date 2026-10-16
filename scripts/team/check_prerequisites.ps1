param([string]$OutputPath)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

function Get-ToolCapability([string]$Name, [string[]]$VersionArgs) {
    $Found = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $Found) { return @{ available = $false; path = $null; version = $null } }
    $Version = $null
    try { $Version = (& $Found.Source @VersionArgs 2>&1 | Select-Object -First 1).ToString() } catch { }
    return @{ available = $true; path = $Found.Source; version = $Version }
}

$Python = @{ available = (Test-Path -LiteralPath $PythonExe); path = $PythonExe; version = $null; packages = @{} }
if ($Python.available) {
    $ProbeCode = "import json,sys,importlib.metadata as m; print(json.dumps(dict(version=sys.version.split()[0],packages={p:m.version(p) for p in ('fastapi','httpx','pytest','pydantic')})))"
    $Info = & $PythonExe -B -c $ProbeCode
    if ($LASTEXITCODE -ne 0) { throw 'Local Python capability probe failed' }
    $Parsed = $Info | ConvertFrom-Json
    $Python.version = $Parsed.version
    $Python.packages = $Parsed.packages
}
$Ports = @{}
foreach ($Port in @(8765,8766,3456,443)) {
    $Listener = $null
    try {
        $Listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $Listener.Start()
        $Ports["$Port"] = 'available_on_loopback'
    } catch { $Ports["$Port"] = 'occupied_or_not_bindable' }
    finally { if ($Listener) { $Listener.Stop() } }
}
$Volumes = @(Get-PSDrive -PSProvider FileSystem | ForEach-Object {
    @{ drive = $_.Name; free_bytes = $_.Free; used_bytes = $_.Used }
})
$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$DpapiAvailable = $false
try {
    Add-Type -AssemblyName System.Security
    $Bytes = [System.Text.Encoding]::UTF8.GetBytes('secretary-synthetic-dpapi-probe')
    $Encrypted = [System.Security.Cryptography.ProtectedData]::Protect($Bytes, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
    $Decrypted = [System.Security.Cryptography.ProtectedData]::Unprotect($Encrypted, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
    $DpapiAvailable = ([Convert]::ToBase64String($Bytes) -eq [Convert]::ToBase64String($Decrypted))
} catch { }
$Unknown = @{ status = 'unknown'; reason = 'Requires owner network information or an authorized external probe' }
$Report = @{
    schema_version = 1; observed_at = [DateTime]::UtcNow.ToString('o'); read_only = $true
    project_root = $ProjectRoot
    host = @{ architecture = $env:PROCESSOR_ARCHITECTURE; is_64_bit_os = [Environment]::Is64BitOperatingSystem
        user_sid = $Identity.User.Value; dpapi_current_user = $DpapiAvailable; volumes = $Volumes }
    runtime = @{ python = $Python; node = (Get-ToolCapability 'node' @('--version'))
        npm = (Get-ToolCapability 'npm.cmd' @('--version')); uv = (Get-ToolCapability 'uv' @('--version'))
        ffmpeg = (Get-ToolCapability 'ffmpeg' @('-version')); ffprobe = (Get-ToolCapability 'ffprobe' @('-version')) }
    ports = $Ports
    network = @{ domain = $Unknown; static_wan_ip = $Unknown; cgnat = $Unknown; nat_permissions = $Unknown
        public_port_443 = $Unknown; public_https = $Unknown; active_route = @{ status = 'unknown'; reason = 'No VPN or routing changes performed' } }
    backup = @{ separate_destination = @{ status = 'unknown'; reason = 'Choose a separate disk or controlled destination with sufficient free space' } }
    owner_checks = @('Confirm router WAN address and CGNAT with ISP', 'Confirm static public IP and domain',
        'Check inbound HTTPS 443 from another network after explicit configuration', 'Choose separate backup destination')
}
$Json = $Report | ConvertTo-Json -Depth 8
if ($OutputPath) {
    $AbsoluteOutput = [System.IO.Path]::GetFullPath($OutputPath)
    $AllowedOutputRoot = (Join-Path $ProjectRoot '.runtime\team-rollout') + '\'
    if (-not $AbsoluteOutput.StartsWith($AllowedOutputRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Capability report must be written under .runtime/team-rollout'
    }
    [System.IO.File]::WriteAllText($AbsoluteOutput, $Json, [System.Text.UTF8Encoding]::new($false))
} else { Write-Output $Json }
