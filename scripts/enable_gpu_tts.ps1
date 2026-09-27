# Run Nova's voice (Kokoro) on the NVIDIA GPU instead of the CPU.
# Swaps the CPU build of onnxruntime for the GPU build (+ its CUDA/cuDNN libraries).
# Safe to re-run. To undo: .venv\Scripts\python -m pip uninstall -y onnxruntime-gpu; .venv\Scripts\python -m pip install onnxruntime
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    Write-Host "No NVIDIA GPU driver found; keeping the CPU voice." -ForegroundColor Yellow
    exit 0
}
# Both packages install the same 'onnxruntime' module, so remove the CPU one first.
.\.venv\Scripts\python -m pip uninstall -y onnxruntime onnxruntime-gpu 2>$null
.\.venv\Scripts\python -m pip install --upgrade "onnxruntime-gpu[cuda,cudnn]>=1.22"
.\.venv\Scripts\python -c "import onnxruntime as o; o.preload_dlls(); print('onnxruntime', o.__version__, o.get_available_providers())"
Write-Host "`nNow run:  .venv\Scripts\python -m assistant ttsbench" -ForegroundColor Green
