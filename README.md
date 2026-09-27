# Orion

A real-time, voice-first personal assistant for Windows. Wake phrase **"Hey Orion"** (coming in Phase 3).
It's calm and concise and calls you "sir", in a British butler style. It runs on your PC and controls it through a tool layer with safety rules.

See [PLAN.md](PLAN.md) for the architecture, chosen stack, costs and phase checklist.

**Current status: Phase 1 (text MVP).** You chat with Claude by text, with streaming replies and tool use. No voice yet.

## Setup (Windows)

Prerequisites: Python 3.12 (`winget install Python.Python.3.12`) and Git.

```powershell
git clone <this repo>
cd Fartbot22
git checkout claude/jarvis-voice-assistant-lhnfza
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

### API key

You need an Anthropic API key (https://console.anthropic.com → API keys). Use either:

- **Windows Credential Manager** (recommended):
  `.venv\Scripts\python -m assistant secrets set ANTHROPIC_API_KEY`
- or `config\.env`: `ANTHROPIC_API_KEY=sk-ant-...`

Keys are never stored in code or committed. `config/.env` is git-ignored.

## Run

```powershell
# Quickest: brain in-process
.venv\Scripts\python -m assistant chat --local --debug

# Or run the core server, then connect clients to it
.venv\Scripts\python -m assistant serve          # terminal 1
.venv\Scripts\python -m assistant chat --debug   # terminal 2
```

`--debug` prints per-turn timings (time to first token, tool time, total) and token usage, including prompt-cache reads and writes.

Other commands:

```powershell
.venv\Scripts\python -m assistant tools      # list tools and their risk levels
.venv\Scripts\python -m assistant audit 20   # last 20 tool calls from the audit log
```

### Things to try

| Say | Tool |
|---|---|
| "What time is it?" | `get_time` |
| "Turn the volume down a bit" / "set volume to 30" / "mute" | `volume` |
| "Open Spotify" / "open Steam" / "open notepad" | `open_app` (aliases in `config/config.yaml`, then Start Menu) |
| "What's on my screen?" / "read the error on my screen" | `look_at_screen` (screenshot → Claude vision) |
| "Who won the game last night?" / "weather in Chicago tomorrow" | `web_search` (Claude server tool) |
| "Work out the monthly payment on a $20k loan at 6% over 5 years" | `escalate` → Claude Opus 5 |

`/reset` clears the conversation. `/quit` exits. `Ctrl+C` interrupts a reply.

## Configuration

Everything lives in `config/config.yaml`: name, honorific, timezone, models, tool aliases, risk overrides and audit log path. Model IDs are only set there.

## Safety

- Every tool has a risk level: `safe` runs immediately, `confirm` asks you first, `blocked` never runs.
  You can override a tool's level in `safety.risk_overrides`.
- Every tool call is appended to `data/audit.jsonl` with the time, client, arguments, result and duration.
- The server only listens on `127.0.0.1` and refuses non-loopback WebSocket clients until Phase 6 adds auth.

## Cost (Anthropic API)

Default chat model: Claude Haiku 4.5 ($1 input / $5 output per million tokens, cached input ~10%).
A voice-style conversation costs roughly **$0.20–0.40 per hour**. Escalations to Opus 5 ($5 / $25 per MTok) cost a few cents each.
Speech runs locally from Phase 2, so it adds nothing. The expected total at ~2 h/day is **$15–32/month**.
See PLAN.md for the breakdown.

## Troubleshooting

| Problem | Fix |
|---|---|
| `No ANTHROPIC_API_KEY` | Set the key (see above). Check `config\.env` has no quotes or spaces. |
| `API key was rejected` | Regenerate the key in the Anthropic console, and check the account has credit. |
| Volume tool errors | `pip install pycaw comtypes` (the installer does this). It needs a default playback device. |
| "couldn't find an app" | Add an alias in `config/config.yaml` → `tools.app_aliases` (an exe name, full path, or URI such as `spotify:`). |
| Screenshot is black | Some games or DRM video block capture. Try windowed mode. |
| Port 8765 in use | Change `server.port` in the config. |

## Development

```bash
python -m pytest -q
```

The tests use a scripted fake Claude client (`tests/fakes.py`), so they need no API key and cost nothing.
