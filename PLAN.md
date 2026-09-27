# ORION — Plan

A real-time, voice-first personal assistant for a Windows gaming laptop.
Wake phrase: **"Hey Orion"**. The name lives only in `config/config.yaml` (`assistant.name`, `assistant.wake_phrase`), so renaming later is a config change plus retraining the wake word. Addresses the owner as **"sir"** (configurable).

## Target machine

| Item | Value | Consequence |
|---|---|---|
| CPU | Intel i9-14900HX (24C/32T, laptop) | Plenty for VAD, wake word, and Kokoro TTS on CPU if needed |
| GPU | RTX 5070 Laptop, 8 GB VRAM (Blackwell, sm_120) | Local STT + TTS fit easily (~2–3 GB). Blackwell needs CUDA 12.8+ builds — verify in Phase 2 |
| RAM | 32 GB DDR5 | No constraint |
| OS | Windows 11 (assumed — confirm) | Win32 / WMI / pycaw for PC control |
| Audio | Mostly headphones | AEC still implemented, speaker mode configurable |
| Budget | ≤ $50 / month | Local speech, Claude Haiku for chat (see cost table) |

## Chosen stack ("fastest and cheapest")

| Layer | Default | Why | Alternative (config switch) |
|---|---|---|---|
| Core | Python 3.12, FastAPI, asyncio, uvicorn | Async end to end | — |
| Wake word | openWakeWord, custom **"hey orion"** model | Free, local, CPU | Push-to-talk only |
| Push-to-talk | `Ctrl+Alt+Space` (hold) | | configurable |
| Kill switch | `Ctrl+Alt+End` + "Orion, stand down" | | configurable |
| VAD | Silero VAD (ONNX, CPU), 400 ms end-of-speech | ~1 ms per frame | tunable 250–700 ms |
| STT | **faster-whisper** `large-v3-turbo` int8_float16 on GPU | Free, ~150–250 ms per utterance on this GPU | Deepgram Nova streaming ($200 free credit, then ~$0.46/h) |
| Brain (chat) | **Claude Haiku 4.5** (`claude-haiku-4-5`), streaming + tools | Lowest time-to-first-token, $1/$5 per MTok | `claude-sonnet-5` |
| Brain (hard tasks) | **Claude Opus 5** (`claude-opus-5`) via an `escalate` hand-off | Deep reasoning when asked | any model id in config |
| TTS | **Kokoro-82M** (local, GPU or CPU) | Free, good quality, ~50–100 ms first audio per sentence | Cartesia Sonic (streaming), Piper |
| AEC | WebRTC APM (`webrtc-audio-processing`) + mic gating during playback | Headphones make this easy; speakers need AEC | gate-only fallback |
| Memory | SQLite + `sqlite-vec`, local embeddings (`bge-small-en`) | One file, no server | Chroma |
| HUD | Tauri + React + Vite | Small, fast, native tray | Electron |
| Remote | PWA over **Tailscale** (`tailscale serve` gives HTTPS on `*.ts.net`, needed for phone mic) | Free, not exposed publicly, no domain needed | Cloudflare Tunnel (needs a domain) |
| Secrets | `.env` + Windows Credential Manager via `keyring` | | |

Model IDs, voices, thresholds, hotkeys and tool permissions all live in `config/config.yaml`. No code has hardcoded model names.

### Estimated monthly cost (60 hours of conversation / month ≈ 2 h/day)

| Item | Estimate |
|---|---|
| Claude Haiku 4.5 conversation (~60 turns/h, cached system+tools) | ~$0.20–0.40 / h → **$12–24** |
| Occasional Opus 5 escalations / screenshots / web search | **~$3–8** |
| STT + TTS (local) | **$0** |
| Tailscale personal plan | **$0** |
| **Total** | **≈ $15–32 / month** — under the $50 cap |

Cloud STT/TTS swaps would add roughly Deepgram ~$0.46/h and Cartesia/ElevenLabs ~$1–4/h of *spoken output*, which would break the budget at heavy use — they stay optional. A speech-to-speech realtime API (OpenAI Realtime / Gemini Live) will be benchmarked behind a flag in Phase 3, but is expected to cost several $/h and is **not** the default.

## Latency budget (target < 800 ms from end-of-speech to first audio)

| Stage | Budget |
|---|---|
| End-of-speech detection (VAD silence) | 300–400 ms (speculative STT starts at 200 ms) |
| STT finalize (GPU) | 100–200 ms |
| Claude Haiku time-to-first-token | 300–500 ms |
| First clause → Kokoro first audio chunk | 60–120 ms |
| **Total** | **~0.8–1.2 s** |

Honest note: 800 ms is tight with a cloud LLM. Tricks to hit it: speculative STT+LLM start before the silence timeout is final (cancelled if the user keeps talking), a very short first clause, a warm HTTP connection, and prompt caching. Every stage is timed and shown in the debug overlay.

## Integrations (opted in)

- **Spotify** — Web API (playback control needs Spotify Premium; falls back to media keys).
- **Browser** — open URLs, search, Playwright for page reading/automation.
- **Discord** — no official API for controlling your own client: mute/deafen via Discord keybinds, open servers/channels via `discord://` links. Sending messages would need a bot account (later, optional).
- **Gmail + Google Calendar** — Google OAuth "desktop app" credentials (free). Sending email is always `confirm`.
- **Home Assistant** — deferred (not wanted for now).

## Safety model

- Every tool declares a risk level: `safe` / `confirm` / `blocked`.
- Delete, send message/email, spend money, arbitrary shell, shutdown → `confirm` minimum.
- Remote clients: shell and file deletion **disabled** by default; stricter policy table in config.
- Append-only JSONL audit log of every tool call (time, client, args, result).
- Kill switch cancels all in-flight tasks and mutes the mic.

## Repository layout

```
assistant/         # Python package (the core)
  core/            # FastAPI server, session manager, orchestrator, config
  voice/           # wakeword, vad, stt/, tts/, aec, audio I/O
  brain/           # Claude client, prompts, model routing
  tools/           # one module per tool group + registry + risk policy
  memory/
routines/          # YAML routine definitions
clients/desktop/   # Tauri HUD
clients/web/       # PWA
config/            # config.yaml, .env.example
tests/
scripts/           # install, run, register-startup
```

## Phase checklist

### Phase 1 — Text MVP
- [x] PLAN.md, repo skeleton, config loader, `.env` + keyring secrets
- [x] Tool registry with JSON schema, risk level, async handler, audit log
- [x] Claude brain: streaming, manual tool loop, prompt caching, model config, escalation hand-off
- [x] 5 safe tools: open app, volume, time, web search (Claude server tool), screenshot-describe
- [x] FastAPI server with WebSocket text chat + CLI client
- [x] Unit tests (tools, registry, brain loop with a fake Claude stream) — 27 passing in CI sandbox (Linux)
- [ ] Verified on the Windows PC with a live API key (volume, open_app, screenshot are Windows-only paths)

### Phase 2 — Voice loop
- [ ] Audio I/O (sounddevice, 16 kHz mono in, 24 kHz out)
- [ ] Silero VAD, configurable silence
- [ ] `STTProvider` interface: faster-whisper (GPU) + Deepgram
- [ ] `TTSProvider` interface: Kokoro + Cartesia + Piper
- [ ] Sentence/clause splitter streaming into TTS
- [ ] Push-to-talk hotkey
- [ ] Per-stage latency breakdown printed each turn
- [ ] Integration test with recorded WAV files

### Phase 3 — Real-time feel
- [ ] Train + ship "hey orion" openWakeWord model
- [ ] Barge-in (stop playback, cancel LLM/TTS)
- [ ] AEC (WebRTC APM) + gating fallback
- [ ] Filler lines for tools > 1 s
- [ ] Speculative end-of-turn
- [ ] Benchmark vs. realtime speech-to-speech API; report latency + cost

### Phase 4 — Full tool layer + safety
- [ ] Apps/windows, system, files, browser (Playwright), keyboard/mouse, clipboard, screen, shell, productivity, Spotify, Discord, Gmail/Calendar
- [ ] Confirmation flow (voice "yes" / UI button)
- [ ] Kill switch, remote policy, routines (YAML), MCP servers as a tool source

### Phase 5 — Desktop HUD + memory
- [ ] Tauri HUD: orb, transcript, tool feed, confirmations, stats, latency overlay, audit log, tray
- [ ] Short-term summarization, long-term SQLite + vectors, "forget that", memory editor

### Phase 6 — Remote access
- [ ] PWA (voice + text + confirmations + quick actions)
- [ ] Tailscale guide (+ Cloudflare Tunnel option)
- [ ] Password + TOTP, QR device pairing, revocable tokens, rate limiting
- [ ] Push notifications

### Phase 7 — Proactive + polish
- [ ] Morning briefing, hardware/disk/download alerts, background research tasks
- [ ] Auto-start, tray, installer, crash recovery, log rotation, offline fallback

## Open questions for the owner
- Confirm Windows 10 vs 11, and paste the tool-check output (Python/Node/Git/ffmpeg/CUDA).
- Spotify Premium? (needed for Web API playback control)
- Happy with the hotkeys above?
