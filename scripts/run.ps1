# Start the Nova core server (Phase 1: localhost only).
Set-Location (Split-Path $PSScriptRoot -Parent)
.\.venv\Scripts\python -m assistant serve
