# Update Nova: pull the latest code, install dependencies, keep the voice on the GPU,
# fetch/rebuild models, and check everything. Use this instead of running pip yourself.
#   powershell -ExecutionPolicy Bypass -File scripts\update.ps1
$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = ".\.venv\Scripts\python.exe"

git pull
if ($LASTEXITCODE -ne 0) { Write-Host "git pull failed; fix that first." -ForegroundColor Red; exit 1 }

& $py -m pip install --quiet -e ".[dev]" 2>&1 | Out-Host

# pip reinstalls the CPU build of onnxruntime (a Kokoro requirement) over the GPU build,
# so the GPU voice must be re-applied after every install.
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    powershell -ExecutionPolicy Bypass -File scripts\enable_gpu_tts.ps1
}

& $py -m assistant models
& $py -m assistant doctor
Write-Host "`nUpdated. Start Nova with:  .venv\Scripts\python -m assistant voice" -ForegroundColor Green
