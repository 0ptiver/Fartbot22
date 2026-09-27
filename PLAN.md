# NOVA — Plan

A real-time, voice-first personal assistant for a Windows gaming laptop.
Wake phrase: **"Hey Nova"**. The name lives only in `config/config.yaml` (`assistant.name`, `assistant.wake_phrase`), so renaming later is a config change plus retraining the wake word. Addresses the owner as **"sir"** (configurable).

## Target machine

| Item | Value | Consequence |
|---|---|---|
| CPU | Intel i9-14900HX (24C/32T, laptop) | Plenty for VAD, wake word, and Kokoro TTS on CPU if needed |
| GPU | RTX 5070 Laptop, 8 GB VRAM (Blackwell, sm_120) | Local STT + TTS fit easily (~2–3 GB). Blackwell needs CUDA 12.8+ builds — verify in Phase 2 |
| RAM | 32 GB DDR5 | No constraint |
| OS | Windows 11 Home (confirmed) | Win32 / WMI / pycaw for PC control |
| Installed | Python 3.12.10, Git 2.54, NVIDIA driver 596.36 | Driver is new enough for Blackwell. No CUDA toolkit needed (pip wheels ship cuBLAS/cuDNN) |
| Missing | Node (Phase 5 HUD), Tailscale (Phase 6), ffmpeg (not needed so far) | Install when those phases start |
| Spotify | Premium (confirmed) | Web API playback control is available |
| Audio | Mostly headphones | AEC still implemented, speaker mode configurable |
| Budget | ≤ $50 / month | Local speech, Claude Haiku for chat (see cost table) |

## Chosen stack ("fastest and cheapest")

| Layer | Default | Why | Alternative (config switch) |
|---|---|---|---|
| Core | Python 3.12, FastAPI, asyncio, uvicorn | Async end to end | — |
| Wake word | openWakeWord, custom **"hey nova"** model | Free, local, CPU | Push-to-talk only |
| Push-to-talk | `Ctrl+Alt+Space` (hold) | | configurable |
| Kill switch | `Ctrl+Alt+End` + "Nova, stand down" | | configurable |
| VAD | Silero VAD (ONNX, CPU), 400 ms end-of-speech | ~1 ms per frame | tunable 250–700 ms |
| STT | **faster-whisper** `large-v3-turbo` int8_float16 on GPU | Free, ~150–250 ms per utterance on this GPU | Deepgram Nova streaming ($200 free credit, then ~$0.46/h) |
| Brain (chat) | **Local `qwen3:4b` in Ollama**, streaming + tools | Free, private, ~0.2–0.4 s to first word on the RTX 5070 | Claude API (`claude-haiku-4-5`) |
| Brain (hard tasks) | **Claude Code CLI on the owner's Claude Pro subscription**, via the `escalate` tool | No API fees; the official, unmodified CLI signs itself in | Claude API (`claude-opus-5`) |
| Vision | Local `qwen3-vl:4b` | Free | Claude Code (subscription) |
| Web search | Free DuckDuckGo (`ddgs`) for the local brain | No key | Claude server tool on the API backend |
| TTS | **Kokoro-82M** (local, GPU or CPU) | Free, good quality, ~50–100 ms first audio per sentence | Cartesia Sonic (streaming), Piper |
| AEC | WebRTC APM (`webrtc-audio-processing`) + mic gating during playback | Headphones make this easy; speakers need AEC | gate-only fallback |
| Memory | SQLite + `sqlite-vec`, local embeddings (`bge-small-en`) | One file, no server | Chroma |
| HUD | Tauri + React + Vite | Small, fast, native tray | Electron |
| Remote | PWA over **Tailscale** (`tailscale serve` gives HTTPS on `*.ts.net`, needed for phone mic) | Free, not exposed publicly, no domain needed | Cloudflare Tunnel (needs a domain) |
| Secrets | `.env` + Windows Credential Manager via `keyring` | | |

Model IDs, voices, thresholds, hotkeys and tool permissions all live in `config/config.yaml`. No code has hardcoded model names.

### Estimated monthly cost

**Default setup (local model + Claude Pro subscription): $0 beyond the existing Pro plan.**
Hard tasks count against Pro usage limits. Anthropic's terms allow signing in to the unmodified Claude Code with your own subscription for ordinary individual use. They don't allow extracting subscription credentials into other apps, so Nova only ever launches the official CLI (source: code.claude.com/docs/en/legal-and-compliance).

Previous API-based estimate, kept for reference (60 hours of conversation / month ≈ 2 h/day):

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

## Security & privacy (applies to every phase)

Goal: nothing about you (keys, accounts, files, screen, voice, memories) can be reached or used by anyone but you. Nova must not be tricked into acting against you either.

**Threats we design against**
1. Someone on the internet reaching Nova → never exposed publicly.
2. A web page you visit talking to Nova's local server → token + Origin + Host checks.
3. Stolen API keys or account tokens → Credential Manager, never in files or code.
4. Prompt injection: a web page, email, document or on-screen text telling Nova to "delete files" or "email this to…" → risky tools always need *your* confirmation.
5. A lost phone or leaked device token → per-device tokens you can revoke, plus 2FA.
6. Nova misbehaving or looping → audit log, kill switch, tool-round cap.

**Controls**

| Area | Control | Status |
|---|---|---|
| Local server | Binds 127.0.0.1 only. Refuses non-loopback peers | ✅ Phase 1 |
| Local server | Every client needs the local token (generated once, stored in Windows Credential Manager) | ✅ Phase 2 |
| Local server | Rejects browser Origins not on the allowlist and unexpected Host headers (DNS rebinding). No API docs exposed | ✅ Phase 2 |
| Secrets | API keys and OAuth tokens in Windows Credential Manager (`keyring`). `.env` is optional and git-ignored | ✅ |
| Spend | Set a monthly spend limit in the Anthropic console, so a leaked key can't run up a bill | 📝 owner action |
| Tools | Risk levels: `safe` / `confirm` / `blocked`. Anything that deletes, sends, spends, runs shell, or shuts down is `confirm` | ✅ framework, Phase 4 tools |
| Tools | Confirmations come from you (voice "yes" or a UI button), never from the model. The prompt shows the exact action and arguments | ✅ framework |
| Tools | Content from web, email, files and screen is treated as untrusted data. It can't lower a tool's risk level | Phase 4 |
| Tools | Shell: allowlisted commands only. Anything else is shown and needs confirmation. Disabled remotely | Phase 4 |
| Tools | File access limited to folders you choose. Deletes go to the Recycle Bin, never permanent | Phase 4 |
| Accounts | Gmail/Calendar/Spotify use OAuth with minimal scopes. Email is read-only unless you enable sending (always confirmed) | Phase 4 |
| Claude hand-off | Launches the official Claude Code only. Your login is never read or stored by Nova. Runs in an empty folder with web search/fetch only: no shell, no edits, no MCP servers. Read is confined to that folder. Deny-by-default permissions. Task passed via stdin. API keys stripped so the subscription is used | ✅ Phase 2.5 |
| Local model | Runs on your PC. Ollama listens on 127.0.0.1 only (its default; don't set `OLLAMA_HOST=0.0.0.0`) | ✅ |
| Audit | Append-only log of every tool call (time, client, args, result summary). Screenshots and email bodies are never written to it | ✅ |
| Kill switch | `Ctrl+Alt+End` or "Nova, stand down" cancels everything and mutes the mic | Phase 4 |
| Voice privacy | Wake word, VAD, STT and TTS run locally. No audio leaves the PC or is saved to disk unless you pick cloud STT | ✅ |
| Memory | Stored locally in your user profile. Encrypted at rest with a key from Credential Manager. "Forget that" deletes for real | Phase 5 |
| Remote | Tailscale only (private network, WireGuard encryption). **No port forwarding, ever** | Phase 6 |
| Remote | Password (argon2 hash) + TOTP 2FA, or QR pairing. Per-device revocable tokens. Login rate limiting and lockout. HTTPS via `tailscale serve` | Phase 6 |
| Remote | Stricter policy: shell and deletion off, more tools need confirmation | ✅ policy hook, Phase 6 |
| Supply chain | Dependencies pinned in a lockfile. MCP servers are opt-in, one by one, and their tools default to `confirm` | Phase 4/7 |
| Data sent to Anthropic | Your messages, tool results and (when asked) screenshots go to the Claude API. Anthropic doesn't train on API data by default | informational |

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
- [x] Audio I/O (sounddevice, 16 kHz mono in, 24 kHz out, instant stop for barge-in)
- [x] Silero VAD (onnxruntime, no torch) + endpointer with pre-roll, hysteresis, configurable silence
- [x] `STTProvider` interface: faster-whisper (GPU, CPU fallback, warm-up) + Deepgram streaming
- [x] `TTSProvider` interface: Kokoro (British voice). Cartesia/Piper deferred (not needed for budget)
- [x] Clause/sentence chunker streaming into TTS (keeps "…, sir." together)
- [x] Push-to-talk hotkey (hold), pressing while it speaks interrupts
- [x] Per-stage latency breakdown printed each turn
- [x] Integration test: recorded WAV -> real Silero VAD -> pipeline (fake STT/TTS/Claude) — 44 tests passing
- [ ] Verified on the Windows PC: Whisper on the RTX 5070 (Blackwell) and Kokoro timings

### Phase 2.5 — Free brain (owner request)
- [x] `LocalBrain` on Ollama (streaming, tool calls, think off, warm-up, keep-alive, think-flag fallback)
- [x] Expert hand-off to Claude Code (subscription): stdin prompt, API keys stripped, `dontAsk`, tools limited to WebSearch/WebFetch/Read (Read confined to an empty workspace), no MCP, no session persistence, timeout + kill
- [x] Local vision for screenshots (`qwen3-vl:4b`) or Claude Code
- [x] Free web search tool (DuckDuckGo) with results marked as untrusted
- [x] Filler line ("One moment, sir.") before slow tools
- [x] `doctor` command. Flags verified against the real Claude Code 2.1.283. 67 tests passing
- [ ] Verified on the Windows PC (`doctor --full`)

### Phase 3 — Real-time feel
- [ ] Train + ship "hey nova" openWakeWord model
- [ ] Barge-in (stop playback, cancel LLM/TTS)
- [ ] AEC (WebRTC APM) + gating fallback
- [x] Filler lines for slow tools (done in 2.5)
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
- Happy with the hotkeys above? (assumed yes until told otherwise)
