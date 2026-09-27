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
| Brain (chat) | **Local `qwen3:4b-instruct-2507` in Ollama**, streaming + tools | Free, private, ~60 ms to first word on the RTX 5070 (measured) | Claude API (`claude-haiku-4-5`) |
| Brain (hard tasks) | **Claude Code CLI on the owner's Claude Pro subscription**, via the `escalate` tool | No API fees; the official, unmodified CLI signs itself in | Claude API (`claude-opus-5`) |
| Vision | Claude Code (subscription): 8 GB VRAM can't hold a vision model beside the chat model | No GPU memory | Local `qwen3-vl:4b` |
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
- [x] Verified on the Windows PC with `doctor --full`: Ollama 0.34.4, Claude Code 2.1.283, subscription hand-off 2.3 s, RTX 5070 detected
- [x] Fix: warm-up now uses the real request settings. Measured on the RTX 5070: model load + prompt cache 6.9 s once at startup, then **58–60 ms to first word**, 100% on GPU
- [x] Fix: `qwen3:4b` is a thinking model (30 s to first word, reasoning leaked into the reply). Default is now `qwen3:4b-instruct-2507-q4_K_M`. A reasoning filter keeps thinking out of speech, and doctor warns about thinking models

### Phase 2.6 — Hands-free by name (owner request: remove the hotkey)
- [x] Owner test: the voice loop works end to end (Whisper on cuda). 1325 ms total, of which 431 ms was opening the speakers and 451 ms was the first TTS audio
- [x] Default mode `wake`: always listening, answers only when "Nova" is near the start/end of the sentence (Whisper hotword bias). 8 s follow-up window, "Yes, sir?" on the name alone
- [x] The `keyboard` hook isn't loaded unless `ptt` mode is chosen (it made keys lag)
- [x] Speakers opened at startup (low-latency stream). Short phrases pre-synthesized and cached
- [x] Speaker-echo guard: brief mic cooldown after speaking, and transcripts matching its own last words are ignored
- [x] Owner test: wake mode works ("answered only after the wake word"). Latency 1.0–1.75 s: VAD 380 ms, STT 260–490 ms, LLM 60–80 ms, TTS 490–750 ms, speaker 1–21 ms (was 431)
- [x] Fix: the small model said "I'll check that, sir" without calling the tool. Removed the "acknowledge first" prompt line, added a one-time nudge when it promises without acting, and "Ask Claude …" now routes straight to the expert
- [x] Fix: follow-up windows chained, so it answered side conversations. Only a named request opens one (6 s)
- [x] `ttsbench` + `voice.tts.kokoro.threads` to tune Kokoro on the hybrid i9. Owner result: CPU is 586–753 ms at every setting (8 threads best)
- [x] Kokoro on the GPU: `device: auto` uses onnxruntime-gpu when present (`scripts/enable_gpu_tts.ps1`, run by the installer on NVIDIA PCs). The int8 model is a CPU fallback. doctor and ttsbench report which is in use
- [x] Owner: onnxruntime-gpu 1.30 (CUDA 13) works on the RTX 5070. Kokoro GPU 210 ms vs CPU 545 ms (int8 on CPU was 2337 ms, so it's dropped from the benchmark)
- [x] Owner test: tools now act (open_app, look_at_screen read an error correctly). Plain reply total 1.07 s. Heuristic cuDNN search was fastest (248 ms)
- [x] Fix: latency report went negative when the filler line was spoken before the model's first word. Tool calls now count as the first model response. The voice CLI shows how long each tool took
- [x] Screen reading: local vision was 8.4 s (4.5 s load) and evicted the chat model (next reply 3.9 s). Default is now `vision: claude_code` (subscription, no VRAM). The screenshot goes in the locked workspace and is deleted after
- [x] Kokoro ~210 ms on the GPU. Profile: STFT runs on the CPU (84.6 ms, no CUDA kernel) plus GPU<->CPU copies; Conv 74 ms on CUDA. fp16 was slower (304 ms). Screen reading via Claude: 6.7 s, and the chat model stays loaded
- [x] `assistant models` rewrites STFT into an equivalent Conv (windowed DFT kernels) → `kokoro-v1.0.gpu.onnx`, kept only if the audio matches the original. Used automatically on the GPU. Rewrite tested against onnxruntime's STFT (all window kinds, one- and two-sided)
- [x] Owner: GPU-only Kokoro **72 ms** (was 243 ms). CPU provider down to 5 ms. STFT maths error 6.8e-7
- [x] Recheck: Kokoro has 0 random ops and is deterministic, but the rewrite's audio differs (0.47). So it's a real difference, and the converted model was removed automatically (Nova is back on the original, 243 ms)
- [x] `models --debug-stft` found it. STFT output matched (6e-7), and the first diverging node was Atan(imag/real), the phase. At the 0 Hz / Nyquist bins the imaginary part is structurally 0: onnxruntime's STFT leaves random ±1e-6 noise there, and the exported atan pattern turns the sign of that noise into ±π
- [x] Fix: exact-zero DFT weights plus a +1e-20 bias on those bins, which gives +π like torch.angle in training. Verified against a reference = original STFT with only those bins set the PyTorch way. `models` also saves original vs fast WAVs for a listening check
- [x] Owner: fast GPU voice sounds fine → Kokoro ~72 ms
- [x] cuDNN algorithm search set to HEURISTIC (the default EXHAUSTIVE re-benchmarks every new sentence length). ttsbench compares heuristic, default and exhaustive on fresh sentences

### Phase 3 — Real-time feel
- [ ] Train + ship "hey nova" openWakeWord model
- [x] Barge-in. `verified` (default, safe with speakers): while Nova talks, speech is transcribed every 0.8 s and interrupts only if its word pairs aren't Nova's own (echo check), or it's a stop word / the name. `fast` (headphones): interrupts on 250 ms of speech. `off`
- [x] Stop words ("stop", "cancel", "never mind") stop without starting a new request. "Stop the music" is treated as a request
- [x] Pause-and-continue: speech during the thinking phase within 2.5 s merges into one request
- [x] Speculative STT: Whisper starts at 160 ms of silence, and the result is used if no speech followed
- [x] Owner test: barge-in works ("actually I lied…" interrupted), speculative STT works, totals 0.95–1.25 s
- [x] Owner found: on speakers Nova heard "90 x 90" from its own "90 times 90 is 8,100" and interrupted itself
- [x] Echo cancellation: WebRTC AEC3 (via `livekit`). Speaker output is the reference, and the mic is cleaned before VAD/Whisper. Simulated room: 33 dB of echo removed, the user talking over it stays 22 dB above the residual
- [x] Backup echo check: normalized text ("x"="times", "8,100"="8100", number words), 4+ character runs, word pairs, short snippets. An all-echo utterance is ignored
- [x] Filler lines for slow tools (done in 2.5)
- [x] Speculative end-of-turn (see speculative STT)
- [ ] Benchmark vs. realtime speech-to-speech API; report latency + cost

### Phase 3 — verified by the owner
- [x] Echo cancellation works on speakers: a whole story with no self-interruption, and "Oh, actually, no" interrupted correctly

### Phase 4 — Full tool layer + safety
- [x] Spoken confirmations for `confirm` tools: "Shall I <action>, sir?", then yes/no (any "no" wins). Unclear → asked again. Silence → cancelled after 12 s. Echo of the question is ignored. Barge-in is paused while waiting. Tools can supply `describe(args)`
- [x] Folder allowlist (`safety.allowed_folders`, OneDrive copies included): resolved paths only (no `..`/symlink escape). Hidden folders, AppData and secret-looking files (passwords, keys, wallets, .env) are always refused
- [x] File tools: find_files, open_file (programs/scripts ask first, never remotely), read_file (labelled "not instructions"), create_note (Documents/Nova Notes), move_file (confirm, never overwrites), delete_file (confirm, Recycle Bin via send2trash). Remote-blocked defaults are now in code, not only in config.yaml
- [x] Timers, reminders, alarms: core/scheduler.py persists to data/reminders.json. Items that came due while Nova was off are announced ("while I was off"). Announced with a chime, never over Nova's own reply, held during standby and delivered on wake-up. Tools set_timer/set_reminder (at a clock time or after a duration)/set_alarm/list_timers/cancel_timer. Fast path for "set a timer for 5 minutes", "cancel the timer", "how long is left"
- [x] Kill switch: "Nova, stand down" / "stop everything" cancels speech, thinking, running tools (kills a Claude Code subprocess) and pending confirmations, then standby. Only "Nova, wake up" / "Nova, I need you" resumes
- [x] Spotify: PKCE login (no secret, loopback callback with state check), refresh-token rotation, search + play (track/artist/album/playlist/liked/recent), control, now playing, queue. Starts the Spotify app when no device is found. Premium/rate-limit errors are spoken plainly
- [x] Media keys for any app (fallback when Spotify isn't linked)
- [x] Speech cleanup: maths symbols, markdown and URLs are spoken as words
- [x] Owner test found: (1) "Nova play my way by Kanye West" ignored as echo because Nova's previous reply contained those words. The old text echo check ran on every utterance forever. Now it's never applied to requests with the name, and only within 2 s after speaking. (2) Tool results weren't visible. They're now shown, and the prompt says to relay them faithfully. (3) Spotify search is title/artist-aware ("X by Y" field search, ranked by artist and title match), and a bare artist name plays the artist
- [x] Live voice latency cause found: `pip install -e .` reinstalled CPU onnxruntime over onnxruntime-gpu (kokoro-onnx requires "onnxruntime"). Added scripts/update.ps1 (re-applies the GPU build after installing) and a loud startup warning when the voice is on the CPU on an NVIDIA PC
- [ ] **Spotify playback: parked (owner's call).** Play requests are "accepted" but nothing plays on LAPTOP-VKHGAOUV (the right device, active). To try next: (1) search with `market=from_token` and skip tracks with `is_playable: false` (likeliest cause: an unplayable track in the owner's country); (2) wait up to ~4 s for the app to start before any transfer, and transfer with `play: true` (the current transfer uses `play: false`, which may stop a starting playback); (3) add a `spotify test` command that plays a known track and prints `/me/player` every 0.5 s. An attempt at this hung the test suite (an unbounded wait loop when `verify_delay` is 0) and was reverted
- [x] Owner: after a failed play, the 4B model kept answering "Spotify isn't open" from history and never called now_playing. Fix: fast path for common commands (what's playing / pause / resume / next / previous / shuffle / play X / liked songs / last song). Recognised by regex, run directly, the tool's result spoken, no model call
- [x] Playback check accepts Spotify's relinked copies (different URI, same song) and reports what's actually playing. This PC's name isn't spoken
- [x] Owner: "Playing My Way by Kanye West" was reported but nothing played. Now it prefers this PC's Spotify app (by computer name; avoids web-player tabs and stale devices), verifies playback via /me/player, transfers and retries once, reports honestly if still silent, and names the device. `spotify devices` shows what Spotify reports
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

### Kokoro GPU rewrite: acceptance
- Rebuild after update: STFT maths 6.8e-7, loudness x1.00, length x1.00, but waveform difference 0.32 → rejected. Cause: Kokoro takes atan(imag/real) of the STFT. Where real ≈ 0, rounding decides its sign (a π phase flip), so no two STFT implementations can match sample-for-sample (the reference is just ORT's rounding)
- Acceptance is now: exact STFT maths, same length, loudness within 5%, and a log-spectrogram distance to the reference no worse than max(1 dB, 1.5× the original model's own distance). `models --debug-stft` compares against the reference and shows the values at the worst positions
