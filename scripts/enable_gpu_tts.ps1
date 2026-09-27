# Run Nova's voice (Kokoro) on the NVIDIA GPU instead of the CPU.
# Swaps the CPU build of onnxruntime for the GPU build (+ its CUDA/cuDNN libraries).
# Safe to re-run. If anything fails, it puts the CPU build back so Nova keeps working.
#
# Note: pip writes warnings to stderr, which Windows PowerShell treats as errors when
# $ErrorActionPreference is "Stop". So we check exit codes instead.
$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = ".\.venv\Scripts\python.exe"

function Restore-Cpu {
    Write-Host "Putting the CPU version back so Nova keeps working..." -ForegroundColor Yellow
    & $py -m pip install --quiet --force-reinstall onnxruntime 2>&1 | Out-Host
}

if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    Write-Host "No NVIDIA GPU driver found; keeping the CPU voice." -ForegroundColor Yellow
    exit 0
}

# Both packages install the same 'onnxruntime' folder, so the CPU one must go first.
$installed = (& $py -m pip list --format=freeze 2>$null) -join "`n"
if ($installed -match "(?m)^onnxruntime==") {
    & $py -m pip uninstall -y onnxruntime 2>&1 | Out-Host
}

& $py -m pip install --upgrade "onnxruntime-gpu[cuda,cudnn]>=1.22" 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Write-Host "Installing onnxruntime-gpu failed." -ForegroundColor Red; Restore-Cpu; exit 1 }

# Re-lay its files in case an earlier CPU uninstall removed shared ones.
& $py -m pip install --quiet --force-reinstall --no-deps "onnxruntime-gpu>=1.22" 2>&1 | Out-Host

& $py -c "import onnxruntime as o; o.preload_dlls(); p = o.get_available_providers(); print('onnxruntime', o.__version__, p); raise SystemExit(0 if 'CUDAExecutionProvider' in p else 3)"
if ($LASTEXITCODE -eq 0) {
    Write-Host "`nGPU voice is ready. Now run:  .venv\Scripts\python -m assistant ttsbench" -ForegroundColor Green
} elseif ($LASTEXITCODE -eq 3) {
    Write-Host "`nInstalled, but CUDA isn't available to onnxruntime. Nova will use the CPU. Run ttsbench to compare." -ForegroundColor Yellow
} else {
    Write-Host "`nonnxruntime-gpu doesn't load on this PC." -ForegroundColor Red
    & $py -m pip uninstall -y onnxruntime-gpu 2>&1 | Out-Host
    Restore-Cpu
    exit 1
}
