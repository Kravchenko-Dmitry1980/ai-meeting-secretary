param(
    [Parameter(Mandatory=$true)][string]$Manifest,
    [Parameter(Mandatory=$true)][string]$RuntimeAccount,
    [Parameter(Mandatory=$true)][string]$CaptureAccount,
    [string]$OutputDirectory,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not [IO.Path]::IsPathRooted($Manifest)) { throw 'absolute_manifest_required' }
# Validate fixed launch paths and hashes before producing reviewable definitions.
$Plan = & $PythonExe -B (Join-Path $PSScriptRoot 'supervisor.py') start --manifest $Manifest
if ($LASTEXITCODE -ne 0) { throw 'lifecycle_manifest_invalid' }
$Config = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
$RuntimeConfig = Get-Content -LiteralPath $Config.runtime_config -Raw | ConvertFrom-Json
$CapturePort = if ($RuntimeConfig.PSObject.Properties.Name -contains 'local_secretary_port') { $RuntimeConfig.local_secretary_port } else { 8765 }
if (($CapturePort -isnot [int] -and $CapturePort -isnot [long]) -or $CapturePort -lt 1024 -or $CapturePort -gt 65535) { throw 'capture_port_invalid' }
if ([IO.Path]::GetFullPath($Config.project_root) -ne [IO.Path]::GetFullPath($ProjectRoot)) { throw 'project_root_mismatch' }
$Id = ([guid]$Config.deployment_id).ToString()
$CurrentAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($CaptureAccount -ine $CurrentAccount) { throw 'capture_requires_original_interactive_account' }
foreach ($Account in @($RuntimeAccount, $CaptureAccount)) {
    if ([string]::IsNullOrWhiteSpace($Account) -or $Account -match '[\r\n\x00]' -or $Account -match '(?i)(^|\\)(SYSTEM|LOCAL SERVICE|NETWORK SERVICE)$' -or $Account -match '^S-1-5-(18|19|20)$') { throw 'account_invalid' }
}
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $Config.state_root ('task-plans\' + [guid]::NewGuid().ToString())
}
if (-not [IO.Path]::IsPathRooted($OutputDirectory)) { throw 'absolute_output_required' }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$AllowedRoot = [IO.Path]::GetFullPath((Join-Path $ProjectRoot '.runtime')) + [IO.Path]::DirectorySeparatorChar
if (-not $OutputDirectory.StartsWith($AllowedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'output_outside_project_runtime' }
$Cursor = $OutputDirectory
while ($Cursor) {
    if (Test-Path -LiteralPath $Cursor) {
        if ((Get-Item -LiteralPath $Cursor).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'reparse_path_forbidden' }
    }
    $Cursor = Split-Path -Parent $Cursor
}
if (Test-Path -LiteralPath $OutputDirectory) { throw 'fresh_output_directory_required' }
[void](New-Item -ItemType Directory -Path $OutputDirectory)
function Escape-Xml([string]$Value) { return [Security.SecurityElement]::Escape($Value) }
function Hash-Text([string]$Value) {
    $Hash = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($Hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value)))).Replace('-', '').ToLowerInvariant() }
    finally { $Hash.Dispose() }
}
function Save-TaskManifest {
    $Temporary = Join-Path $OutputDirectory ('.write-' + [guid]::NewGuid().ToString('N').Substring(0,16) + '.tmp')
    $Bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Export | ConvertTo-Json -Depth 8))
    try {
        $Stream = [IO.File]::Open($Temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $Stream.Write($Bytes,0,$Bytes.Length); $Stream.Flush($true) } finally { $Stream.Dispose() }
        if ([IO.File]::Exists($RecordPath)) { [IO.File]::Replace($Temporary,$RecordPath,[Management.Automation.Language.NullString]::Value) }
        else { [IO.File]::Move($Temporary,$RecordPath) }
    } finally {
        if ([IO.File]::Exists($Temporary)) { [IO.File]::Delete($Temporary) }
    }
}
function Add-RegistrationEvent([hashtable]$Definition,[string]$Phase) {
    $Export.registration_events += @{ name=$Definition.name; path=$Definition.path; kind=$Definition.kind; phase=$Phase; observed_at=[DateTime]::UtcNow.ToString('o') }
    Save-TaskManifest
}
$PowerShellExe = Join-Path $PSHOME 'powershell.exe'
if (-not (Test-Path -LiteralPath $PowerShellExe -PathType Leaf)) { $PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe' }
$TaskPath = '\SecretaryTeam\'
$Definitions = @()
foreach ($Kind in @('runtime', 'capture')) {
    $TaskName = 'Secretary-Team-' + $Id + '-' + $Kind
    $Account = if ($Kind -eq 'runtime') { $RuntimeAccount } else { $CaptureAccount }
    $LogonType = if ($Kind -eq 'runtime') { 'Password' } else { 'InteractiveToken' }
    $Trigger = if ($Kind -eq 'runtime') { '<BootTrigger><Enabled>true</Enabled><Delay>PT30S</Delay></BootTrigger>' } else { '<LogonTrigger><Enabled>true</Enabled><UserId>' + (Escape-Xml $CaptureAccount) + '</UserId></LogonTrigger>' }
    $Script = if ($Kind -eq 'runtime') { Join-Path $PSScriptRoot 'start.ps1' } else { Join-Path $ProjectRoot 'scripts\start.ps1' }
    $Arguments = if ($Kind -eq 'runtime') {
        '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $Script + '" -Manifest "' + $Manifest + '" -Apply -Supervise'
    } else {
        '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $Script + '" -NoBrowser -NoBootstrap -Port ' + $CapturePort + ' -TeamRuntimeConfig "' + $Config.runtime_config + '"'
    }
    $Description = 'Secretary Team lifecycle schema1 deployment=' + $Id + ' component=' + $Kind
    $Restart = if ($Kind -eq 'runtime') { '<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>' } else { '' }
    $Xml = '<?xml version="1.0" encoding="UTF-16"?><Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><RegistrationInfo><Description>' + (Escape-Xml $Description) + '</Description></RegistrationInfo><Triggers>' + $Trigger + '</Triggers><Principals><Principal id="Author"><UserId>' + (Escape-Xml $Account) + '</UserId><LogonType>' + $LogonType + '</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals><Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>false</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><Enabled>true</Enabled><Hidden>true</Hidden><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>' + $Restart + '</Settings><Actions Context="Author"><Exec><Command>' + (Escape-Xml $PowerShellExe) + '</Command><Arguments>' + (Escape-Xml $Arguments) + '</Arguments><WorkingDirectory>' + (Escape-Xml $ProjectRoot) + '</WorkingDirectory></Exec></Actions></Task>'
    [void][xml]$Xml
    $XmlPath = Join-Path $OutputDirectory ($Kind + '.xml')
    [IO.File]::WriteAllText($XmlPath, $Xml, [Text.Encoding]::Unicode)
    $Definitions += @{ name=$TaskName; path=$TaskPath; kind=$Kind; xml_path=$XmlPath; xml_sha256=(Hash-Text $Xml); description=$Description }
}
$Export = @{ schema_version=1; deployment_id=$Id; manifest_path=$Manifest; applied=$false; application_state='preview'; definitions=$Definitions; registration_events=@(); registered=@(); installed=@() }
$RecordPath = Join-Path $OutputDirectory 'tasks-manifest.json'
Save-TaskManifest
if (-not $Apply) {
    Write-Output (@{ mode='preview'; manifest=$RecordPath; task_count=2; requires=@('runtime_account_batch_logon_right', 'local_credential_prompt_on_apply', 'owner_review_of_both_xml_files') } | ConvertTo-Json -Compress)
    exit 0
}
# Owner activation only. Existing tasks are never replaced or adopted.
foreach ($Definition in $Definitions) {
    if (Get-ScheduledTask -TaskPath $TaskPath -TaskName $Definition.name -ErrorAction SilentlyContinue) { throw 'scheduled_task_already_exists' }
}
$Credential = Get-Credential -UserName $RuntimeAccount -Message 'Register the reviewed Team runtime task. Password remains local.'
if ($null -eq $Credential -or $Credential.UserName -ine $RuntimeAccount) { throw 'runtime_credential_required' }
$Installed = @()
$RegistrationPhase = 'not_started'
try {
    foreach ($Definition in $Definitions) {
        $Xml = [IO.File]::ReadAllText($Definition.xml_path)
        if ((Hash-Text $Xml) -ne $Definition.xml_sha256) { throw 'task_plan_changed' }
        $Export.application_state='applying'
        $RegistrationPhase='started'
        # Durable before invoking a mutating API. A crash after this event has
        # unknown outcome and must not be mistaken for "nothing was installed".
        Add-RegistrationEvent $Definition 'registration_started'
        if ($Definition.kind -eq 'runtime') {
            $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Credential.Password)
            try {
                $Password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
                $null = Register-ScheduledTask -TaskPath $TaskPath -TaskName $Definition.name -Xml $Xml -User $RuntimeAccount -Password $Password
            } finally {
                $Password = $null
                [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
            }
        } else {
            $null = Register-ScheduledTask -TaskPath $TaskPath -TaskName $Definition.name -Xml $Xml
        }
        $RegistrationPhase='registered'
        $Export.applied=$true
        $Export.registered += @{ name=$Definition.name; path=$TaskPath; kind=$Definition.kind; description=$Definition.description; requested_xml_sha256=$Definition.xml_sha256; registered_xml_sha256=$null }
        Add-RegistrationEvent $Definition 'registration_succeeded'
        $ActualXml = Export-ScheduledTask -TaskPath $TaskPath -TaskName $Definition.name
        $Installed += @{ name=$Definition.name; path=$TaskPath; kind=$Definition.kind; description=$Definition.description; registered_xml_sha256=(Hash-Text $ActualXml) }
        $Export.installed=$Installed
        $RegistrationPhase='verified'
        Add-RegistrationEvent $Definition 'ownership_verified'
    }
    $Export.application_state='installed'
    Save-TaskManifest
} catch {
    $Export.application_state='incomplete'
    try {
        if ($RegistrationPhase -eq 'started') { Add-RegistrationEvent $Definition 'registration_uncertain' }
        elseif ($RegistrationPhase -eq 'registered') { Add-RegistrationEvent $Definition 'export_failed' }
        else { Save-TaskManifest }
    } catch { } # The prior flushed started/succeeded record remains authoritative.
    Write-Output (@{ mode='incomplete'; error_code='task_registration_failed'; manifest=$RecordPath } | ConvertTo-Json -Compress)
    exit 2
}
Write-Output (@{ mode='installed'; manifest=$RecordPath; task_count=$Installed.Count } | ConvertTo-Json -Compress)
