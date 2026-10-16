param([Parameter(Mandatory=$true)][string]$Manifest, [switch]$Apply)
$ErrorActionPreference = 'Stop'
# Manifest is the exact tasks-manifest.json produced by install_tasks.ps1.
if (-not [IO.Path]::IsPathRooted($Manifest)) { throw 'absolute_manifest_required' }
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$FullPath = [IO.Path]::GetFullPath($Manifest)
$AllowedRoot = [IO.Path]::GetFullPath((Join-Path $ProjectRoot '.runtime')) + [IO.Path]::DirectorySeparatorChar
if (-not $FullPath.StartsWith($AllowedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'manifest_outside_project_runtime' }
$Cursor = $FullPath
while ($Cursor) {
    if (Test-Path -LiteralPath $Cursor) {
        if ((Get-Item -LiteralPath $Cursor).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'reparse_path_forbidden' }
    }
    $Cursor = Split-Path -Parent $Cursor
}
$Record = Get-Content -LiteralPath $FullPath -Raw | ConvertFrom-Json
if ($Record.schema_version -ne 1) { throw 'task_manifest_invalid' }
$Id = ([guid]$Record.deployment_id).ToString()
$Unverified = @()
foreach ($Event in $Record.registration_events) {
    if ($Event.phase -eq 'registration_started') {
        $Verified = @($Record.installed | Where-Object { $_.name -eq $Event.name -and $_.path -eq $Event.path })
        if ($Verified.Count -eq 0) { $Unverified += @{ name=$Event.name; path=$Event.path; error_code='registration_requires_reconciliation' } }
    }
}
if ($Apply -and $Unverified.Count -gt 0) { throw 'task_reconciliation_required' }
function Hash-Text([string]$Value) {
    $Hash = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($Hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value)))).Replace('-', '').ToLowerInvariant() }
    finally { $Hash.Dispose() }
}
$Targets = @()
foreach ($Definition in $Record.installed) {
    if ($Definition.kind -notin @('runtime','capture') -or $Definition.name -ne ('Secretary-Team-' + $Id + '-' + $Definition.kind) -or $Definition.path -ne '\SecretaryTeam\' -or $Definition.registered_xml_sha256 -notmatch '^[0-9a-f]{64}$') { throw 'task_ownership_invalid' }
    $Task = Get-ScheduledTask -TaskPath $Definition.path -TaskName $Definition.name -ErrorAction SilentlyContinue
    if ($null -eq $Task) { continue }
    $Xml = Export-ScheduledTask -TaskPath $Definition.path -TaskName $Definition.name
    if ((Hash-Text $Xml) -ne $Definition.registered_xml_sha256 -or $Task.Description -ne $Definition.description) { throw 'task_ownership_changed' }
    if ($Apply -and $Task.State -eq 'Running') { throw 'stop_owned_runtime_before_uninstall' }
    $Targets += $Definition
}
if (-not $Apply) {
    Write-Output (@{ mode='preview'; deployment_id=$Id; remove_tasks=@($Targets | ForEach-Object { $_.name }); reconciliation_required=$Unverified; delete_data=$false } | ConvertTo-Json -Depth 5 -Compress)
    exit 0
}
foreach ($Definition in $Targets) {
    # Recheck immediately before the destructive Scheduler API operation.
    if ((Hash-Text (Export-ScheduledTask -TaskPath $Definition.path -TaskName $Definition.name)) -ne $Definition.registered_xml_sha256) { throw 'task_ownership_changed' }
    $CurrentTask = Get-ScheduledTask -TaskPath $Definition.path -TaskName $Definition.name -ErrorAction Stop
    if ($null -eq $CurrentTask -or $CurrentTask.Description -ne $Definition.description) { throw 'task_ownership_changed' }
    if ($CurrentTask.State -eq 'Running') { throw 'stop_owned_runtime_before_uninstall' }
    Unregister-ScheduledTask -TaskPath $Definition.path -TaskName $Definition.name -Confirm:$false
}
Write-Output (@{ mode='uninstalled'; deployment_id=$Id; task_count=$Targets.Count; data_preserved=$true } | ConvertTo-Json -Compress)
