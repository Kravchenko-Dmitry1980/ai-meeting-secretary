param([string]$PublicUrl, [switch]$ReadOnly)
$ErrorActionPreference = 'Stop'

# This entry point never configures listeners, routing, certificates or accounts.
# Only the exact Python child created below is stopped on its fixed deadline.
function Write-FailureReport([string]$Code) {
    $Unknown = @{ status = 'unknown'; code = 'requires_separate_evidence' }
    $Report = @{
        schema_version = 1; observed_at = [DateTime]::UtcNow.ToString('o')
        read_only = [bool]$ReadOnly; vantage = 'current_host'; status = 'probe_failed'
        target = $null; error_code = $Code
        dns = $Unknown; tcp443 = $Unknown; tls = $Unknown; http = $Unknown
        external_reachability = $Unknown; cgnat = $Unknown; static_wan_ip = $Unknown
        nat_permissions = $Unknown; mobile_network = $Unknown; max_callback = $Unknown
        limitations = @('current_host_observation_is_not_external_reachability')
        owner_checks = @('choose_explicit_public_https_url', 'verify_from_another_network')
    }
    if ($Code -in @('read_only_required','public_url_invalid')) { $Report.status = 'invalid_input' }
    Write-Output ($Report | ConvertTo-Json -Depth 5 -Compress)
}

if (-not $ReadOnly) {
    Write-FailureReport 'read_only_required'
    exit 2
}
if ($PublicUrl.Length -gt 2048) {
    Write-FailureReport 'public_url_invalid'
    exit 2
}
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Helper = Join-Path $PSScriptRoot 'check_ingress.py'
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf) -or -not (Test-Path -LiteralPath $Helper -PathType Leaf)) {
    Write-FailureReport 'project_runtime_missing'
    exit 2
}
$Process = $null
$Started = $false
try {
    $Info = [System.Diagnostics.ProcessStartInfo]::new()
    $Info.FileName = $PythonExe
    # Windows filenames cannot contain a quote; helper path does not end in a slash.
    $Info.Arguments = '-B "' + $Helper + '" --read-only --stdin-url'
    $Info.UseShellExecute = $false
    $Info.CreateNoWindow = $true
    $Info.RedirectStandardInput = $true
    $Info.RedirectStandardOutput = $true
    $Info.RedirectStandardError = $true
    $Process = [System.Diagnostics.Process]::new()
    $Process.StartInfo = $Info
    if (-not $Process.Start()) { throw 'helper_start_failed' }
    $Started = $true
    $Output = $Process.StandardOutput.ReadToEndAsync()
    $Errors = $Process.StandardError.ReadToEndAsync()
    $Payload = if ([string]::IsNullOrEmpty($PublicUrl)) { 'null' } else { ConvertTo-Json -InputObject $PublicUrl -Compress }
    $Process.StandardInput.Write($Payload)
    $Process.StandardInput.Close()
    if (-not $Process.WaitForExit(15000)) {
        $Process.Kill()
        [void]$Process.WaitForExit(1000)
        Write-FailureReport 'probe_deadline_exceeded'
        exit 2
    }
    $Text = $Output.GetAwaiter().GetResult()
    [void]$Errors.GetAwaiter().GetResult() # Raw diagnostics are never returned.
    if ($Text.Length -gt 16384 -or $Process.ExitCode -notin @(0,2)) { throw 'helper_response_invalid' }
    $Parsed = $Text | ConvertFrom-Json
    if ($Parsed.schema_version -ne 1 -or -not $Parsed.read_only -or $Parsed.vantage -ne 'current_host') {
        throw 'helper_response_invalid'
    }
    Write-Output $Text.Trim()
    exit $Process.ExitCode
} catch {
    try {
        if ($Started -and $Process -and -not $Process.HasExited) {
            $Process.Kill()
            [void]$Process.WaitForExit(1000)
        }
    } catch { }
    Write-FailureReport 'probe_helper_unavailable'
    exit 2
} finally {
    if ($Process) { $Process.Dispose() }
}
