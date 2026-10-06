param(
    [string]$TargetPath,
    [string]$GpgPath = 'C:\Program Files\Git\usr\bin\gpg.exe'
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $PSScriptRoot)))
$ManifestPath = Join-Path $ProjectRoot 'config\team\integration-manifest.json'
$Manifest = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
$Pin = $Manifest.vikunja
$RuntimeRoot = Join-Path $ProjectRoot '.runtime\team'
function Assert-NoReparseAncestors([string]$Candidate) {
    $Current = [IO.Path]::GetFullPath($Candidate)
    if (-not $Current.StartsWith($ProjectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Path outside project root'
    }
    while ($Current -and $Current.Length -ge $ProjectRoot.Length) {
        if (Test-Path -LiteralPath $Current) {
            $Item = Get-Item -Force -LiteralPath $Current
            if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Project-local setup refuses reparse-point ancestors'
            }
        }
        if ($Current -eq $ProjectRoot) { break }
        $Current = Split-Path -Parent $Current
    }
}
if (-not $TargetPath) { $TargetPath = Join-Path $RuntimeRoot ('vikunja-' + $Pin.version) }
$TargetPath = [IO.Path]::GetFullPath($TargetPath)
if (-not $TargetPath.StartsWith($RuntimeRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Target must be a new directory within project .runtime\team'
}
Assert-NoReparseAncestors $TargetPath
if (Test-Path -LiteralPath $TargetPath) { throw 'destination_exists: installation preserved' }
if (-not (Test-Path -LiteralPath $GpgPath -PathType Leaf)) { throw 'GPG unavailable; supply an existing GPG path' }
if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'Pinned artifact requires Windows AMD64' }
$DownloadRoot = Join-Path $RuntimeRoot ('downloads\vikunja-' + $Pin.version)
Assert-NoReparseAncestors $DownloadRoot
New-Item -ItemType Directory -Force -Path $DownloadRoot | Out-Null
$ArchivePath = Join-Path $DownloadRoot $Pin.artifact
$SignaturePath = $ArchivePath + '.sig'
$KeyPath = Join-Path $DownloadRoot 'gpg.key'
foreach ($Source in @(
    @{ Url = $Pin.source_url; Path = $ArchivePath },
    @{ Url = $Pin.signature_url; Path = $SignaturePath },
    @{ Url = $Pin.key_url; Path = $KeyPath }
)) {
    $SourceUri = [Uri]$Source.Url
    if ($SourceUri.Scheme -ne 'https' -or $SourceUri.Host -ne 'dl.vikunja.io') { throw 'Untrusted download origin' }
    Assert-NoReparseAncestors $Source.Path
    # Existing downloads are reverified by hash AND signature, never trusted by presence.
    if (-not (Test-Path -LiteralPath $Source.Path)) {
        Invoke-WebRequest -UseBasicParsing -Uri $Source.Url -OutFile $Source.Path
    }
}
& (Join-Path $ProjectRoot '.venv\Scripts\python.exe') -B (Join-Path $PSScriptRoot 'install_vikunja.py') `
    --archive $ArchivePath --target $TargetPath --manifest $ManifestPath `
    --gpg $GpgPath --key $KeyPath --signature $SignaturePath --allowed-root $RuntimeRoot
if ($LASTEXITCODE -ne 0) { throw 'Verified project-local installation refused' }
