param(
    [Parameter(Mandatory)][ValidateSet('export','verify','import')][string]$Operation,
    [Parameter(Mandatory)][string]$Package,
    [Parameter(Mandatory)][string]$KeyFile,
    [string]$DataDir,
    [string]$TargetData
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Schema = 'secretary.voice-bank-transfer.v1'
$MaxPlain = 32 * 1024 * 1024
$MaxEnvelope = 1400000
$MaxCipher = $MaxEnvelope + 65536
$Magic = [Text.Encoding]::ASCII.GetBytes('SVBANK01')
$Aad = [Text.Encoding]::ASCII.GetBytes($Schema)
$CurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User

function Hash-Bytes([byte[]]$Value) {
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($Value)).ToLowerInvariant()
}

function Assert-PlainPath([string]$Path, [bool]$FileExpected = $false) {
    $full = [IO.Path]::GetFullPath($Path)
    $item = if ($FileExpected) { [IO.FileInfo]::new($full) } else { [IO.DirectoryInfo]::new($full) }
    while ($null -ne $item) {
        if ($item.Exists -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'unsafe_path' }
        $item = if ($item -is [IO.FileInfo]) { $item.Directory } else { $item.Parent }
    }
}

function Write-PrivateFile([string]$Path, [byte[]]$Value) {
    Assert-PlainPath $Path $true
    $security = [Security.AccessControl.FileSecurity]::new()
    $security.SetAccessRuleProtection($true, $false)
    $security.SetOwner($CurrentSid)
    $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $CurrentSid, [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow))
    $stream = [IO.FileSystemAclExtensions]::Create([IO.FileInfo]::new($Path),
        [IO.FileMode]::CreateNew, [Security.AccessControl.FileSystemRights]::FullControl,
        [IO.FileShare]::None, 4096, [IO.FileOptions]::WriteThrough, $security)
    try { $stream.Write($Value, 0, $Value.Length); $stream.Flush($true) }
    finally { $stream.Dispose() }
}

function Read-PrivateKey {
    Assert-PlainPath $KeyFile $true
    $acl = [IO.FileSystemAclExtensions]::GetAccessControl([IO.FileInfo]::new($KeyFile))
    if (-not $acl.AreAccessRulesProtected -or $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $CurrentSid.Value) {
        throw 'key_permissions_not_private'
    }
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.IdentityReference.Value -ne $CurrentSid.Value -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { throw 'key_permissions_not_private' }
    }
    if ([IO.FileInfo]::new($KeyFile).Length -ne 32) { throw 'invalid_key' }
    return ,([IO.File]::ReadAllBytes($KeyFile))
}

function Assert-Entry($Entry) {
    $expected = @('relative_path','profile_id','enrollment_id','material_revision','source_ciphertext_sha256','plaintext_sha256','envelope_base64')
    if ($Entry.Count -ne $expected.Count) { throw 'invalid_entry' }
    foreach ($name in $expected) { if (-not $Entry.ContainsKey($name)) { throw 'invalid_entry' } }
    foreach ($name in @('profile_id','enrollment_id')) {
        if ([Guid]::ParseExact($Entry[$name], 'D').ToString('D') -cne $Entry[$name]) { throw 'invalid_identifier' }
    }
    if ($Entry.relative_path -cne ('voice/profiles/' + $Entry.profile_id + '/' + $Entry.enrollment_id + '.dpapi')) { throw 'invalid_path' }
    if ($Entry.material_revision -isnot [long] -and $Entry.material_revision -isnot [int]) { throw 'invalid_revision' }
    if ($Entry.material_revision -lt 0 -or $Entry.material_revision -gt 9007199254740992) { throw 'invalid_revision' }
    foreach ($name in @('source_ciphertext_sha256','plaintext_sha256')) {
        if ($Entry[$name] -cnotmatch '^[0-9a-f]{64}$') { throw 'invalid_hash' }
    }
    $plain = [Convert]::FromBase64String($Entry.envelope_base64)
    if ($plain.Length -gt $MaxEnvelope -or $plain.Length -eq 0 -or (Hash-Bytes $plain) -cne $Entry.plaintext_sha256) { throw 'invalid_envelope' }
    $envelope = [Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json -AsHashtable -Depth 20
    if ($envelope.version -ne 1 -or $envelope.profile_id -cne $Entry.profile_id -or
        $envelope.enrollment_id -cne $Entry.enrollment_id -or $envelope.material_revision -ne $Entry.material_revision) { throw 'envelope_identity_mismatch' }
    return ,$plain
}

$key = $null
$plainTransport = $null
$aes = $null
try {
    if (-not $IsWindows -or $PSVersionTable.PSVersion -lt [version]'7.4') { throw 'windows_powershell74_required' }
    Assert-PlainPath $Package $true
    $entries = [Collections.Generic.List[object]]::new()
    if ($Operation -eq 'export') {
        if ([IO.File]::Exists($Package) -or [IO.File]::Exists($KeyFile)) { throw 'destination_exists' }
        $requestText = [Console]::In.ReadToEnd()
        if ($requestText.Length -gt 65536) { throw 'request_limit' }
        $request = $requestText | ConvertFrom-Json -AsHashtable -Depth 10
        if ($request.entries.Count -lt 1 -or $request.entries.Count -gt 32) { throw 'entry_limit' }
        foreach ($record in $request.entries) {
            $path = Join-Path $DataDir $record.relative_path
            Assert-PlainPath $path $true
            $info = [IO.FileInfo]::new($path)
            if ($info.Length -gt $MaxCipher) { throw 'envelope_limit' }
            $ciphertext = [IO.File]::ReadAllBytes($path)
            if ((Hash-Bytes $ciphertext) -cne $record.source_ciphertext_sha256) { throw 'source_changed' }
            $plain = [Security.Cryptography.ProtectedData]::Unprotect($ciphertext, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
            try {
                $entry = @{
                    relative_path = $record.relative_path; profile_id = $record.profile_id
                    enrollment_id = $record.enrollment_id; material_revision = $record.material_revision
                    source_ciphertext_sha256 = $record.source_ciphertext_sha256
                    plaintext_sha256 = (Hash-Bytes $plain); envelope_base64 = [Convert]::ToBase64String($plain)
                }
                $validated = Assert-Entry $entry
                [Security.Cryptography.CryptographicOperations]::ZeroMemory($validated)
                $entries.Add($entry)
            } finally { [Security.Cryptography.CryptographicOperations]::ZeroMemory($plain) }
        }
        $payload = @{schema=$Schema; source_backup_manifest_sha256=$request.source_backup_manifest_sha256; entries=$entries.ToArray()}
        $plainTransport = [Text.Encoding]::UTF8.GetBytes(($payload | ConvertTo-Json -Depth 12 -Compress))
        if ($plainTransport.Length -gt $MaxPlain) { throw 'transport_limit' }
        $key = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
        $nonce = [Security.Cryptography.RandomNumberGenerator]::GetBytes(12)
        $tag = [byte[]]::new(16)
        $cipher = [byte[]]::new($plainTransport.Length)
        $aes = [Security.Cryptography.AesGcm]::new($key, 16)
        $aes.Encrypt($nonce, $plainTransport, $cipher, $tag, $Aad)
        $transport = [byte[]]::new(36 + $cipher.Length)
        [Array]::Copy($Magic, 0, $transport, 0, 8)
        [Array]::Copy($nonce, 0, $transport, 8, 12)
        [Array]::Copy($tag, 0, $transport, 20, 16)
        [Array]::Copy($cipher, 0, $transport, 36, $cipher.Length)
        Write-PrivateFile $KeyFile $key
        Write-PrivateFile $Package $transport
    } else {
        $key = Read-PrivateKey
        $length = [IO.FileInfo]::new($Package).Length
        if ($length -le 36 -or $length -gt $MaxPlain + 36) { throw 'transport_limit' }
        $transport = [IO.File]::ReadAllBytes($Package)
        if ([Text.Encoding]::ASCII.GetString($transport, 0, 8) -cne 'SVBANK01') { throw 'invalid_magic' }
        $nonce = [byte[]]::new(12)
        $tag = [byte[]]::new(16)
        $cipher = [byte[]]::new($transport.Length - 36)
        [Array]::Copy($transport, 8, $nonce, 0, 12)
        [Array]::Copy($transport, 20, $tag, 0, 16)
        [Array]::Copy($transport, 36, $cipher, 0, $cipher.Length)
        $plainTransport = [byte[]]::new($cipher.Length)
        $aes = [Security.Cryptography.AesGcm]::new($key, 16)
        $aes.Decrypt($nonce, $cipher, $tag, $plainTransport, $Aad)
        $payload = [Text.Encoding]::UTF8.GetString($plainTransport) | ConvertFrom-Json -AsHashtable -Depth 15
        if ($payload.Count -ne 3 -or $payload.schema -cne $Schema -or
            -not $payload.ContainsKey('source_backup_manifest_sha256') -or -not $payload.ContainsKey('entries') -or
            $payload.entries.Count -lt 1 -or $payload.entries.Count -gt 32) { throw 'invalid_payload' }
        if ($null -ne $payload.source_backup_manifest_sha256 -and $payload.source_backup_manifest_sha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'invalid_backup_hash' }
        $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        foreach ($entry in $payload.entries) {
            $plain = Assert-Entry $entry
            [Security.Cryptography.CryptographicOperations]::ZeroMemory($plain)
            if (-not $seen.Add($entry.relative_path)) { throw 'duplicate_entry' }
            $entries.Add($entry)
        }
    }
    if ($Operation -eq 'import') {
        Assert-PlainPath $TargetData
        if ([IO.Directory]::Exists($TargetData) -or [IO.File]::Exists($TargetData)) { throw 'target_exists' }
        # Only a new isolated subtree is created; no application DB or live state is opened.
        [IO.Directory]::CreateDirectory($TargetData) | Out-Null
    }
    $receipt = [Collections.Generic.List[object]]::new()
    foreach ($entry in $entries) {
        $row = @{
            relative_path=$entry.relative_path; profile_id=$entry.profile_id; enrollment_id=$entry.enrollment_id
            material_revision=$entry.material_revision; source_ciphertext_sha256=$entry.source_ciphertext_sha256
            plaintext_sha256=$entry.plaintext_sha256
        }
        if ($Operation -eq 'import') {
            $plain = Assert-Entry $entry
            try {
                $rewrapped = [Security.Cryptography.ProtectedData]::Protect($plain, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
                $roundtrip = [Security.Cryptography.ProtectedData]::Unprotect($rewrapped, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
                try { if ((Hash-Bytes $roundtrip) -cne $entry.plaintext_sha256) { throw 'rewrap_mismatch' } }
                finally { [Security.Cryptography.CryptographicOperations]::ZeroMemory($roundtrip) }
                $destination = Join-Path $TargetData $entry.relative_path
                [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
                Write-PrivateFile $destination $rewrapped
                $row.target_ciphertext_sha256 = Hash-Bytes $rewrapped
            } finally { [Security.Cryptography.CryptographicOperations]::ZeroMemory($plain) }
        }
        $receipt.Add($row)
    }
    @{schema='secretary.voice-bank-transfer-receipt.v1'; operation=$Operation; status='CRYPTO_VERIFIED';
      package_sha256=(Hash-Bytes $transport); source_backup_manifest_sha256=$payload.source_backup_manifest_sha256;
      files=$receipt.ToArray(); activation_performed=$false} | ConvertTo-Json -Depth 10 -Compress
} catch {
    # Fixed labels, exception type and source line only; never exception messages/data.
    [Console]::Error.WriteLine(('voice_transport_failed:{0}:{1}' -f $_.Exception.GetType().Name, $_.InvocationInfo.ScriptLineNumber))
    exit 1
} finally {
    if ($null -ne $aes) { $aes.Dispose() }
    if ($null -ne $key) { [Security.Cryptography.CryptographicOperations]::ZeroMemory($key) }
    if ($null -ne $plainTransport) { [Security.Cryptography.CryptographicOperations]::ZeroMemory($plainTransport) }
}
