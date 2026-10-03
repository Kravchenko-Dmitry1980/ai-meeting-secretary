param([switch]$DownloadModel)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VoiceEnvironment = Join-Path $ProjectRoot '.venv-voice'
$VoicePython = Join-Path $VoiceEnvironment 'Scripts\python.exe'
$RuntimeRoot = Join-Path $ProjectRoot '.runtime\voice'
$CacheVariables = @{
    UV_CACHE_DIR = (Join-Path $RuntimeRoot 'uv-cache')
    HF_HOME = (Join-Path $RuntimeRoot 'hf-cache')
    TORCH_HOME = (Join-Path $RuntimeRoot 'torch-cache')
    XDG_CACHE_HOME = (Join-Path $RuntimeRoot 'cache')
    TEMP = (Join-Path $RuntimeRoot 'tmp')
    TMP = (Join-Path $RuntimeRoot 'tmp')
}
$PreviousVariables = @{}
foreach ($Entry in $CacheVariables.GetEnumerator()) {
    $PreviousVariables[$Entry.Key] = [Environment]::GetEnvironmentVariable($Entry.Key, 'Process')
    New-Item -ItemType Directory -Force -Path $Entry.Value | Out-Null
    [Environment]::SetEnvironmentVariable($Entry.Key, $Entry.Value, 'Process')
}
try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'uv must already be installed; no global installation is performed.' }
    if (-not (Test-Path -LiteralPath $VoicePython)) {
        $ExistingPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $ExistingPython)) { throw 'Bootstrap the project Python 3.12 environment first.' }
        & uv venv --python $ExistingPython --no-python-downloads $VoiceEnvironment
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated voice environment.' }
    }
    & $VoicePython -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 is required"'
    if ($LASTEXITCODE -ne 0) { throw 'Wrong voice interpreter version.' }
    & uv pip sync (Join-Path $ProjectRoot 'config\voice-runtime\requirements.lock') --python $VoicePython --torch-backend cpu --require-hashes --no-python-downloads
    if ($LASTEXITCODE -ne 0) { throw 'Pinned voice dependencies could not be installed.' }
    & uv pip check --python $VoicePython
    if ($LASTEXITCODE -ne 0) { throw 'Voice dependency check failed.' }
    $ModelArguments = @((Join-Path $ProjectRoot 'config\voice-runtime\prepare_model.py'))
    if ($DownloadModel) { $ModelArguments += '--download' }
    & $VoicePython @ModelArguments
    if ($LASTEXITCODE -ne 0) { throw 'Pinned model is not prepared.' }
    Write-Output 'Optional CPU voice runtime ready. Speaker recognition quality still requires participant recordings.'
} finally {
    foreach ($Entry in $PreviousVariables.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($Entry.Key, $Entry.Value, 'Process')
    }
}
