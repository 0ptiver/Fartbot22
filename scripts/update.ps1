# Update Nova: pull the latest code, install dependencies, keep the voice on the GPU,
# fetch/rebuild models, and check everything. Use this instead of running pip yourself.
#   powershell -ExecutionPolicy Bypass -File scripts\update.ps1
$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = ".\.venv\Scripts\python.exe"

# A running Nova (tray / started with Windows) locks its files: stop it for the update.
$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*-m assistant*" -and $_.ProcessId -ne $PID }
$wasRunning = [bool]$running
if ($wasRunning) {
    Write-Host "Stopping Nova for the update..."
    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

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

$autostart = (& $py -m assistant autostart status) -like "*starts with Windows*"
if ($wasRunning -or $autostart) {
    Start-Process -FilePath ".\.venv\Scripts\pythonw.exe" -ArgumentList "-m", "assistant", "background" -WorkingDirectory (Get-Location)
    Write-Host "`nUpdated. Nova is starting again in the tray (icon by the clock)." -ForegroundColor Green
} else {
    Write-Host "`nUpdated. Start Nova with:  .venv\Scripts\python -m assistant voice" -ForegroundColor Green
    Write-Host "Or keep it running in the tray, starting with Windows:  .venv\Scripts\python -m assistant autostart on" -ForegroundColor Green
}
