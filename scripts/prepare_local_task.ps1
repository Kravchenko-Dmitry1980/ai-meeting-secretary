param(
    [Parameter(Mandatory=$true)][string]$DataDir,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [ValidateRange(1024,65535)][int]$Port = 8765
)

# Preview only. Registration and process launch are deliberately separate steps.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Get-SafeAbsolutePath([string]$Value) {
    if ($Value -notmatch '^[A-Za-z]:[\\/]' -or $Value -match '[\x00-\x1f"<>|?*]' -or
        $Value.Substring(2).Contains(':') -or ($Value -split '[\\/]') -contains '..') {
        throw 'absolute_local_path_required'
    }
    $full = [IO.Path]::GetFullPath($Value).TrimEnd('\')
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -Force -LiteralPath $cursor
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'reparse_path_forbidden' }
        }
        $parent = Split-Path -Parent $cursor
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
    return $full
}

function Test-ChildPath([string]$Path, [string]$Parent) {
    return $Path.StartsWith($Parent.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Escape-Xml([string]$Value) { return [Security.SecurityElement]::Escape($Value) }

$project = Get-SafeAbsolutePath (Split-Path -Parent $PSScriptRoot)
$data = Get-SafeAbsolutePath $DataDir
$runtime = Get-SafeAbsolutePath (Join-Path $project '.runtime')
$output = Get-SafeAbsolutePath $OutputDirectory
$python = Get-SafeAbsolutePath (Join-Path $project '.venv\Scripts\pythonw.exe')
$supervisor = Get-SafeAbsolutePath (Join-Path $project 'scripts\local_supervisor.py')
if (-not (Test-ChildPath $data $project) -or -not (Test-Path -LiteralPath $data -PathType Container)) {
    throw 'existing_project_data_directory_required'
}
if (-not (Test-ChildPath $output $runtime) -or (Test-ChildPath $output $data)) {
    throw 'preview_output_must_be_runtime_child_outside_data'
}
if (Test-Path -LiteralPath $output) { throw 'preview_output_already_exists' }
if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
    -not (Test-Path -LiteralPath $supervisor -PathType Leaf)) { throw 'local_runtime_files_missing' }

$account = [Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $account.User.Value
$sha = [Security.Cryptography.SHA256]::Create()
try { $projectHash = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($project.ToLowerInvariant())) | ForEach-Object { $_.ToString('x2') }) }
finally { $sha.Dispose() }
$taskName = 'Secretary-Local-' + $projectHash.Substring(0,12)
$taskPath = '\SecretaryLocal\'
$arguments = '-B "' + $supervisor + '" run --data-dir "' + $data + '" --port ' + $Port + ' --runtime-root "' + $runtime + '"'
$created = [DateTime]::UtcNow.ToString('o')
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Date>$(Escape-Xml $created)</Date><Author>$(Escape-Xml $account.Name)</Author><Description>Secretary Offline local supervision; preview requires owner approval before registration.</Description><URI>$(Escape-Xml ($taskPath + $taskName))</URI></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>$(Escape-Xml $sid)</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Owner"><UserId>$(Escape-Xml $sid)</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>false</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>true</Hidden><RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>false</WakeToRun><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Priority>7</Priority></Settings>
  <Actions Context="Owner"><Exec><Command>$(Escape-Xml $python)</Command><Arguments>$(Escape-Xml $arguments)</Arguments><WorkingDirectory>$(Escape-Xml $project)</WorkingDirectory></Exec></Actions>
</Task>
"@
# Validate XML before creating anything. Output is a new directory, never an overwrite.
$parsed = [xml]$xml
$null = New-Item -ItemType Directory -Path $output
$xmlPath = Join-Path $output 'local-task.xml'
$manifestPath = Join-Path $output 'preview.json'
$utf8 = [Text.UTF8Encoding]::new($false)
# Task Scheduler accepts XmlText as a Unicode BSTR; keep the declaration aligned.
[IO.File]::WriteAllText($xmlPath, $xml, [Text.Encoding]::Unicode)
$sha = [Security.Cryptography.SHA256]::Create()
try { $xmlHash = -join ($sha.ComputeHash([IO.File]::ReadAllBytes($xmlPath)) | ForEach-Object { $_.ToString('x2') }) }
finally { $sha.Dispose() }
$manifest = [ordered]@{
    schema_version = 1; mode = 'preview'; applied = $false; created_at_utc = $created
    project = $project; data_dir = $data; runtime_root = $runtime; port = $Port
    task_name = $taskName; task_path = $taskPath; account = $account.Name; account_sid = $sid
    xml_path = $xmlPath; xml_sha256 = $xmlHash
    offline = $true; logon_required = $true; restart_delays_seconds = @(1,2,4)
    scheduler_restart_on_failure = $false; microphone_auto_start = $false
    executable = $python; arguments = $arguments
}
[IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 5), $utf8)
[ordered]@{mode='preview'; applied=$false; manifest=$manifestPath; xml_path=$xmlPath} | ConvertTo-Json -Compress
