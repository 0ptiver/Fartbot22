# Nova installer for Windows (PowerShell). Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Need($cmd, $hint) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { Write-Host "MISSING: $cmd  ->  $hint" -ForegroundColor Yellow; return $false }
    return $true
}

$ok = Need "py" "winget install Python.Python.3.12"
if ($ok) {
    $v = & py -3.12 --version 2>$null
    if (-not $v) { Write-Host "Python 3.12 not found -> winget install Python.Python.3.12" -ForegroundColor Yellow; exit 1 }
    Write-Host "Using $v"
} else { exit 1 }

if (-not (Test-Path .venv)) { py -3.12 -m venv .venv }
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev]"

if (-not (Test-Path config\.env)) { Copy-Item config\.env.example config\.env; Write-Host "Created config\.env - add your ANTHROPIC_API_KEY (or use: .venv\Scripts\python -m assistant secrets set ANTHROPIC_API_KEY)" }

Write-Host "`nDownloading speech models (Silero VAD ~2 MB, Kokoro TTS ~340 MB, Whisper large-v3-turbo ~1.6 GB)..."
.\.venv\Scripts\python -m assistant models

.\.venv\Scripts\python -m pytest -q
Write-Host "`nInstalled. Try:" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\python -m assistant chat --local --debug   (text)"
Write-Host "  .\.venv\Scripts\python -m assistant voice                  (hold Ctrl+Alt+Space and talk)"
