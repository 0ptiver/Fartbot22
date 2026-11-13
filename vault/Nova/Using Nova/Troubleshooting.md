---
tags: [nova, howto, troubleshooting]
---
# Troubleshooting

First: send `data\logs\nova.log` and a screenshot.

| Problem | Fix |
|---|---|
| PowerShell blank after `assistant background`, "frozen" | Normal: Nova is running and output goes to the log. Closing the window stops it. Use `pythonw` instead. |
| Nova won't start; log says **"Not an input device: 'Speakers (Realtek(R) Audio)'"** | The saved mic number now points at the speakers (Windows renumbers devices when headsets come and go). Nova now falls back to the default mic. Fix it for good: `notepad config\local.yaml` → `input_device: fifine` / `Logitech` / `null`. |
| "Ollama isn't running" | Start the Ollama app. Check with `ollama list`. |
| "Claude Code isn't signed in" / **"OAuth session expired and could not be refreshed"** | In PowerShell: `claude` → `/login` → "Claude account with subscription" → Authorize → `/exit`. Test: `claude -p "say hi"`. Still failing: `claude logout`, then log in again. |
| **"Claude Code failed: exit 1"** on long jobs | It ran out of steps. Fixed: jobs now carry on in a fresh run (up to 4 runs). Update. |
| "usage limit has been reached" | The Pro plan's limit. It resets after a few hours; everyday chat still works locally. |
| Voice slow (~650 ms per sentence) | Someone ran pip install. `powershell -ExecutionPolicy Bypass -File scripts\enable_gpu_tts.ps1` |
| Reads its reasoning aloud | Wrong model. Use `qwen3:4b-instruct-2507-q4_K_M`. |
| Ignores you | The voice lock: lower the strictness on the Voice tab, or "learn my voice again" for a new mic. |
| Answers other people | Needs the name; turn on the voice lock. |
| Phone: "Tailscale isn't connected" | Open Tailscale on the PC and sign in. |
| "Not your voice (match 0.64, needs 0.66)" in the log | A near miss: lower the strictness slightly. |
| Window (HUD) won't open: "port in use" | Nova is already running: `.venv\Scripts\python -m assistant hud`. |
| Whisper on CPU | Update the NVIDIA driver; it still works, slower. |
| Screenshot black | Some games/DRM block capture; try windowed mode. |
