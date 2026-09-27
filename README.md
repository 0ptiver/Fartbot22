# Nova

A real-time, voice-first personal assistant for Windows. Wake phrase **"Hey Nova"** (coming in Phase 3).
It's calm and concise and calls you "sir", in a British butler style. It runs on your PC and controls it through a tool layer with safety rules.

See [PLAN.md](PLAN.md) for the architecture, chosen stack, costs and phase checklist.

**Current status: Phase 2 (voice loop).** Hold a hotkey and talk. The everyday model, speech-to-text and text-to-speech all run on your PC for free. Hard tasks go to your Claude subscription.

## How it thinks: free by default

| Job | Who does it | Cost |
|---|---|---|
| Everyday chat, PC control, quick facts, web lookups | **A local model in Ollama** (`qwen3:4b-instruct-2507`) on your GPU | free |
| "What's on my screen?" | **A local vision model** (`qwen3-vl:4b`) | free |
| Hard tasks: research, analysis, writing, code, "ask Claude…" | **Your Claude Pro subscription**, through the official Claude Code CLI | included in Pro |
| Optional: the Claude API instead of either one | `brain.backend: anthropic` / `expert.backend: anthropic` | paid per token |

How the hand-off works: Nova runs the real, unmodified `claude` program, signed in with your own account. Nova never sees or stores your Claude login.
Hard tasks count against your Pro plan's usage limits, so keep them for things that need it.
Claude Code is locked down for this: it gets web search and web fetch only, no shell, no file edits, no MCP servers, and it runs in an empty folder of its own.

## Setup (Windows)

You need:
- Python 3.12 (`winget install Python.Python.3.12`) and Git
- **Ollama** (https://ollama.com/download)
- **Claude Code** (PowerShell: `irm https://claude.ai/install.ps1 | iex`). Run `claude` once and log in with your Claude account

```powershell
git clone https://github.com/0ptiver/Fartbot22
cd Fartbot22
git checkout claude/jarvis-voice-assistant-lhnfza
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
.venv\Scripts\python -m assistant doctor --full
```

The installer pulls the Ollama models and speech models (about 8 GB in total) and runs the tests.
`doctor` checks everything and tells you exactly what to fix. `--full` also sends one tiny test request through each model.

### API key (optional)

Only needed if you switch a backend to `anthropic`. Store it in Windows Credential Manager:
`.venv\Scripts\python -m assistant secrets set ANTHROPIC_API_KEY`.
Keys are never stored in code or committed. When Nova calls Claude Code, it strips any API key from Claude Code's environment, so your subscription is used rather than the API.

## Run

```powershell
# Quickest: brain in-process
.venv\Scripts\python -m assistant chat --local --debug

# Or run the core server, then connect clients to it
.venv\Scripts\python -m assistant serve          # terminal 1
.venv\Scripts\python -m assistant chat --debug   # terminal 2
```

`--debug` prints per-turn timings (time to first token, tool time, total) and token usage.

Other commands:

```powershell
.venv\Scripts\python -m assistant tools      # list tools and their risk levels
.venv\Scripts\python -m assistant audit 20   # last 20 tool calls from the audit log
```

## Voice (Phase 2)

```powershell
.venv\Scripts\python -m assistant models     # one-off: downloads the speech models (~2 GB, installer does this)
.venv\Scripts\python -m assistant voice      # hold Ctrl+Alt+Space, talk, release
```

- **Push-to-talk** (default): hold `Ctrl+Alt+Space` while you talk and release when you're done. Pressing it while Nova is talking cuts Nova off.
- **Open mic**: `python -m assistant voice --mode open_mic` listens all the time and answers when you pause. Use headphones: until echo cancellation lands in Phase 3, it stops listening while it's talking. The "Hey Nova" wake word also comes in Phase 3.
- **Latency**: after every reply it prints how long each stage took and the total time from when you stopped talking to when it started speaking.
- **Test without a mic**: `python -m assistant voice --wav my_question.wav --out reply.wav` uses a recording as the mic and saves the spoken reply.
- `python -m assistant devices` lists microphones and speakers. Set `voice.input_device` / `voice.output_device` in the config to pick one.

Speech stack (all switchable in `config/config.yaml` → `voice`):

| Stage | Default | Notes |
|---|---|---|
| Voice detection | Silero VAD | `end_silence_ms: 400` is how long a pause ends your turn |
| Speech-to-text | faster-whisper `large-v3-turbo` on your GPU | falls back to CPU automatically if CUDA fails. `small.en` is faster but less accurate |
| Text-to-speech | Kokoro, voice `bm_george` (British male) | also `bm_lewis`, `bm_daniel`, `bf_emma`. Runs on the CPU so the GPU stays free for Whisper |
| Cloud option | Deepgram (`stt.provider: deepgram`) | needs `DEEPGRAM_API_KEY`, ~$0.46/hour |

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

## Security

The full plan is in [PLAN.md → Security & privacy](PLAN.md#security--privacy-applies-to-every-phase). The short version:

- **Nothing is exposed to the internet.** The core server listens on `127.0.0.1` only. Remote access (Phase 6) uses Tailscale plus a password and 2FA, never port forwarding.
- **Only your own apps can talk to it.** Clients must present a local token kept in Windows Credential Manager. Web pages are blocked by Origin and Host checks, so a site you visit can't drive Nova.
- **Keys live in Windows Credential Manager**, not in files or code.
- **Risky actions need your yes.** Each tool is `safe`, `confirm` or `blocked`, and you can tighten any of them in `safety.risk_overrides`. Text from web pages, emails or your screen can't approve an action. Only you can.
- **Everything is logged.** Each tool call is appended to `data/audit.jsonl` (`python -m assistant audit`).
- **Voice stays on your PC.** Speech recognition and synthesis run locally, and no audio is saved.

## Cost

With the defaults (local model plus your Claude Pro subscription), Nova costs **nothing beyond your Pro plan**. The only other costs are your electricity and GPU.
If you switch to the Claude API: Haiku 4.5 conversation is roughly $0.20–0.40 per hour, and Opus escalations cost a few cents each. PLAN.md has the details.

### Choosing the local model (8 GB VRAM)

| Model | VRAM | Notes |
|---|---|---|
| `qwen3:4b-instruct-2507-q4_K_M` (default) | ~3 GB | answers straight away and leaves room for Whisper and the vision model |
| `qwen3:8b` | ~5.5 GB | smarter (Nova turns its "thinking" off). Pair it with `stt.whisper.model: small.en` or `distil-large-v3` so everything fits |
| `qwen3-vl:4b` (vision) | ~3.5 GB | loaded only when you ask about your screen. Set `local.vision: claude_code` to use your subscription instead |

Change `brain.local.model` in `config/config.yaml`, then run `ollama pull <model>`.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Ollama isn't running" | Start the Ollama app (tray icon). Check with `ollama list`. |
| "model … isn't downloaded" | `ollama pull qwen3:4b-instruct-2507-q4_K_M` (or whatever `brain.local.model` says). |
| "Claude Code isn't signed in" | Run `claude` in a terminal, log in, then `/exit`. |
| "usage limit has been reached" | Your Pro plan's limit for now. It resets after a few hours. Everyday chat keeps working locally. |
| Slow first word, or it reads its reasoning aloud ("Okay, the user asked…") | You're on a "thinking" model. Use `qwen3:4b-instruct-2507-q4_K_M`. `doctor` warns about this. |
| Local model picks the wrong tool, or rambles | Try `qwen3:8b` (see the table above), or say "ask Claude …" to force the hand-off. |
| `No ANTHROPIC_API_KEY` | Only relevant with an `anthropic` backend. Set the key, or switch back to `local` / `claude_code`. |
| Volume tool errors | `pip install pycaw comtypes` (the installer does this). It needs a default playback device. |
| "couldn't find an app" | Add an alias in `config/config.yaml` → `tools.app_aliases` (an exe name, full path, or URI such as `spotify:`). |
| Screenshot is black | Some games or DRM video block capture. Try windowed mode. |
| Port 8765 in use | Change `server.port` in the config. |
| `whisper on cuda failed` in the log | It still works on the CPU, just slower. Update the NVIDIA driver, then `pip install -U ctranslate2 nvidia-cublas-cu12 "nvidia-cudnn-cu12>=9,<10"`. As a quick test, set `stt.whisper.model: small.en`. |
| Hotkey does nothing | Another app may own `Ctrl+Alt+Space`. Change `voice.ptt_hotkey` (e.g. `right ctrl`). Some games block global hotkeys unless the assistant runs as admin. |
| It cuts you off mid-sentence (open mic) | Raise `voice.vad.end_silence_ms` to 600. |
| "(heard nothing)" | The message says why: keys released too soon, a silent mic, or unclear audio. Run `python -m assistant mictest --input N` to see the level and what Whisper hears. Also check Settings → Privacy & security → Microphone → "Let desktop apps access your microphone". |
| Voice sounds robotic or too fast | Change `voice.tts.kokoro.voice` / `speed`. |

## Development

```bash
python -m pytest -q
```

The tests use a scripted fake Claude client (`tests/fakes.py`), so they need no API key and cost nothing.
The voice integration test runs a recorded clip (`tests/data/what_time.wav`) through the real Silero VAD and the whole pipeline, with fake STT and TTS.
