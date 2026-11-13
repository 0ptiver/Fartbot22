---
tags: [nova, howto]
---
# Start, stop, update

All commands are run in PowerShell from Nova's folder:
```powershell
cd C:\Users\patto\Fartbot22
```

## Update (also restarts Nova in the tray)
```powershell
powershell -ExecutionPolicy Bypass -File scripts\update.ps1
```
> [!danger] Never run `pip install` directly
> It reinstalls CPU onnxruntime over the GPU one and Nova's voice slows to ~650 ms. If that happened: `powershell -ExecutionPolicy Bypass -File scripts\enable_gpu_tts.ps1`.

## Start
| How | Command |
|---|---|
| In the tray, no window (normal) | `.venv\Scripts\pythonw -m assistant background` |
| In this window, to see what it's doing | `.venv\Scripts\python -m assistant voice` (quit the tray one first: only one Nova at a time) |
| Start with Windows | `.venv\Scripts\python -m assistant autostart on` |
| Reopen the HUD window | `.venv\Scripts\python -m assistant hud` |

`python -m assistant background` (with **python**, not pythonw) keeps PowerShell blank while Nova runs. That's normal, but closing that window stops Nova.

## Stop
- Right-click the tray dot → **Quit**.
- Or force it: `taskkill /f /im pythonw.exe`

## Check everything
```powershell
.venv\Scripts\python -m assistant doctor          # quick check
.venv\Scripts\python -m assistant doctor --full   # also tests Claude + the models (uses a tiny bit of the plan)
```

## Other commands
`devices` (list mics/speakers) · `mictest` · `ttsbench` · `models` · `tools` · `routines` · `audit 20` · `chat` · `serve` · `spotify login|logout|devices|test` · `secrets set NAME`

## Logs
`data\logs\nova.log` (send this when something breaks). The supervisor writes `data\logs\supervisor.log`.

## Rebuild this vault
```powershell
powershell -ExecutionPolicy Bypass -File scripts\vault.ps1
```
Copies the latest notes from the repo into `C:\Users\patto\ObsidianClaude\Nova`.
