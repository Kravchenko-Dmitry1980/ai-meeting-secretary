param([switch]$SkipFrontend)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$env:UV_CACHE_DIR = Join-Path $ProjectRoot '.runtime\uv-cache'
$env:npm_config_cache = Join-Path $ProjectRoot '.runtime\npm-cache'
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot '.runtime') | Out-Null
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'Install uv separately, then rerun bootstrap. No global install is performed.' }
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot '.venv\Scripts\python.exe'))) {
    & uv venv --python 3.12 --no-python-downloads (Join-Path $ProjectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 must be available locally. No global Python installation is performed.' }
}
& uv sync --frozen --no-python-downloads
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
$PythonExe = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
& $PythonExe -c 'import sys; print(sys.executable)'
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot '.env'))) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot '.env.example') -Destination (Join-Path $ProjectRoot '.env')
}
if (-not $SkipFrontend) {
    Push-Location -LiteralPath (Join-Path $ProjectRoot 'frontend')
    try {
        & npm.cmd ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    } finally { Pop-Location }
}
Write-Output 'Secretary dependencies ready. Run scripts\start.ps1.'
