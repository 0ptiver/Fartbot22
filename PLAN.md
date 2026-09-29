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
- [x] PC control (tools/pc.py): system_status (CPU, memory, GPU temp/usage via nvidia-smi, battery, disk, uptime), lock_pc, power (sleep/restart/shutdown: confirm, remote-blocked, restart/shutdown wait 60 s), cancel_shutdown, window (focus/minimize/maximize/restore/close via WM_CLOSE so apps can ask to save, list, show desktop), open_website (known sites, YouTube/Amazon/Maps/Google search, http/https only). Fast path for "lock my PC", "cancel the shutdown", "show the desktop". Not yet tried on the PC
- [x] Kill switch: "Nova, stand down" / "stop everything" cancels speech, thinking, running tools (kills a Claude Code subprocess) and pending confirmations, then standby. Only "Nova, wake up" / "Nova, I need you" resumes
- [x] Spotify: PKCE login (no secret, loopback callback with state check), refresh-token rotation, search + play (track/artist/album/playlist/liked/recent), control, now playing, queue. Starts the Spotify app when no device is found. Premium/rate-limit errors are spoken plainly
- [x] Media keys for any app (fallback when Spotify isn't linked)
- [x] Speech cleanup: maths symbols, markdown and URLs are spoken as words
- [x] Owner test found: (1) "Nova play my way by Kanye West" ignored as echo because Nova's previous reply contained those words. The old text echo check ran on every utterance forever. Now it's never applied to requests with the name, and only within 2 s after speaking. (2) Tool results weren't visible. They're now shown, and the prompt says to relay them faithfully. (3) Spotify search is title/artist-aware ("X by Y" field search, ranked by artist and title match), and a bare artist name plays the artist
- [x] Live voice latency cause found: `pip install -e .` reinstalled CPU onnxruntime over onnxruntime-gpu (kokoro-onnx requires "onnxruntime"). Added scripts/update.ps1 (re-applies the GPU build after installing) and a loud startup warning when the voice is on the CPU on an NVIDIA PC
- [x] (superseded by polish pass 3: success now needs Windows to show it playing, with an in-app Play fallback) **Spotify playback: parked (owner's call).** Play requests are "accepted" but nothing plays on LAPTOP-VKHGAOUV (the right device, active). To try next: (1) search with `market=from_token` and skip tracks with `is_playable: false` (likeliest cause: an unplayable track in the owner's country); (2) wait up to ~4 s for the app to start before any transfer, and transfer with `play: true` (the current transfer uses `play: false`, which may stop a starting playback); (3) add a `spotify test` command that plays a known track and prints `/me/player` every 0.5 s. An attempt at this hung the test suite (an unbounded wait loop when `verify_delay` is 0) and was reverted
- [x] Owner: after a failed play, the 4B model kept answering "Spotify isn't open" from history and never called now_playing. Fix: fast path for common commands (what's playing / pause / resume / next / previous / shuffle / play X / liked songs / last song). Recognised by regex, run directly, the tool's result spoken, no model call
- [x] Playback check accepts Spotify's relinked copies (different URI, same song) and reports what's actually playing. This PC's name isn't spoken
- [x] Owner: "Playing My Way by Kanye West" was reported but nothing played. Now it prefers this PC's Spotify app (by computer name; avoids web-player tabs and stale devices), verifies playback via /me/player, transfers and retries once, reports honestly if still silent, and names the device. `spotify devices` shows what Spotify reports
- [ ] Browser (Playwright), keyboard/mouse, clipboard, shell, Discord, Gmail/Calendar
- [ ] Confirmation buttons in the HUD (voice yes/no is done)
- [x] Routines (tools/routines.py): named in config (`routines:`; built-ins gaming_mode, movie_time, heading_out, goodnight; own ones in local.yaml, `null` switches one off). Exact phrase match on the fast path ("Nova, gaming mode", "start gaming mode"), plus a run_routine tool for the model. Every step goes through registry.execute, so confirmations, remote blocks and the audit log apply per step. Steps: tool+args, wait (≤30 s), optional (failure not mentioned). `python -m assistant routines` lists and checks them. Not yet tried on the PC
- [ ] MCP servers as a tool source

### Phase 5 — Desktop HUD + memory
- [x] HUD (assistant/hud/): a local web app served from inside `assistant voice` (same event loop, so it sees every voice event and presses the loop's buttons directly), shown in an Edge `--app` window with its own browser profile (no extensions, no shared cookies). Orb (colour/motion per state, reacts to mic level), live conversation, typed requests, activity feed (tools, reminders, errors, optional "ignored speech"), Yes/No confirmation card with countdown (Y/N keys), Stop / Stand down / Wake up, mic mute, wake-word vs open-mic switch, timers with live countdowns and cancel, routine buttons. `python -m assistant hud` reopens it. Security: 127.0.0.1 only, per-run random key in the URL fragment, Host + Origin checks, strict CSP (own scripts only), textContent only. Chose a web page over Tauri: nothing to compile or install on the owner's PC. Not yet tried on the PC
- [x] HUD works on the owner's PC (their screenshot). Same test found: the local model said "unpausing YouTube and minimizing the PowerShell window is done, the video is now fullscreen" without calling any tool, then ignored the nudge and said its next reply twice, run together ("shortly.I will"). Fixes: false "done" claims are nudged too (only when an action was asked for, so "it's closed on Sundays" is just an answer); the retry after a nudge is held back and replaced by "Sorry, sir, I wasn't able to do that." if it still doesn't act; a space between rounds; prompt says one tool per action, all in one reply; new press_key tool (allowlisted single keys, optional app focus first: 'f' in chrome = fullscreen the video). press_key and window are remote-blocked
- [x] Voice mouse grid (tools/grid.py), asked for by the owner after Nova said "I can't see your screen" to "hit play on the video on my screen". "Show the grid" draws a numbered 10x6 grid over the main screen (Tk overlay on its own thread: see-through, click-through, never takes focus, DPI-aware so cells match real pixels); "zoom N" splits a cell 3x3, "back", "click/double click/right click N", "move to N", "scroll down (a lot)", "drag 5 to 12", "hide the grid". Clicking hides it. All on the fast path (no model); while the grid shows, these work without saying "Nova". Also: "hit play/pause the video" -> media key; the prompt says Nova can see the screen, press keys and click. mouse/mouse_grid are remote-blocked. Overlay previewed under Xvfb here; not yet tried on Windows
- [x] Owner confirmed the grid lines up on their screen (screenshot). Multiple screens: the grid opens on the screen the mouse is on; "show the grid on screen 2" / "on the other monitor" / "left screen"; with the grid up, "next screen" / "switch to the main screen". Screen 1 = the main screen, then left to right; screens left of the main one have negative coordinates (tested). Not yet tried with a real second screen
- [x] Owner: the grid works on screen 2. Then "Can you full screen the video and press play" got "I don't know which video you mean". New video tool (tools/video.py): Windows' media sessions (GSMTC via winrt, the list the volume pop-up shows) say which video is playing/paused and in which app, so play/pause are exact (not toggles) and skip Spotify; key actions (fullscreen f, exit Esc, mute m, skip l/j) focus the browser window whose title contains the video's title (else a video-site tab, never the Nova HUD) and press the key. Fast path splits "X and Y" into ordered actions when a video/fullscreen is mentioned; bare "pause"/"next song" still go to Spotify. Falls back to the media key when Windows lists no video. winrt is untestable here (fakes only); not yet tried on the PC
- [x] Owner: "overhaul his general control functions so I can just sit here and use the PC through him". Added: keyboard (type_text via SendInput unicode; press_keys with spoken key names, repeats; ~30 spoken shortcuts: copy/paste/undo/save, tabs, back/forward, refresh, switch windows, snap, move to other screen, start menu, task manager, screenshot); typing or Enter into a terminal / Run box asks first and is remote-blocked; "this window" = the active window, never the Nova HUD; click by name via UI Automation (comtypes, one FindAllBuildCache call; exact > prefix > all words > fuzzy, kind words "button/box/link" steer; ties show numbers on just those); "show numbers" labels everything clickable in the active window, then "click 7" or just "7"; dictation mode (everything typed, no name needed; "new line", "scratch that", "stop dictation"; "Nova, ..." still a command; switches off after 2 min quiet; HUD shows it). "Go back" is now browser back. UIA and SendInput can't run here: logic tested with fakes only
- [ ] Click by description when UI Automation can't see it (games): screenshot with the grid -> vision picks the cell ("click the play button"): screenshot with the grid drawn on it -> vision picks the cell
- [ ] HUD extras: settings editor, audit log view, latency overlay, tray icon
- [x] Long-term memory (core/memory.py): SQLite + FTS5 (porter) in data/memory.db, on this PC only. Only what the user asks to remember; passwords/PINs/card/account numbers refused; "my sister's birthday is June 4" replaces "... June 3" (same subject), "I like sushi" doesn't replace "I like pizza". Relevant facts (all while there are <= 8) go into the local model's per-turn <context>; never to Claude. Tools remember/recall/forget (forget names what goes and asks first; remote-blocked); fast path "remember that ...", "what do you remember (about X)", "forget that/about X/everything"
- [x] Brain view in the HUD (owner asked for "a centre like a brain that shows everything in the database"): canvas with a procedural glowing brain (colour follows Nova's state, pulses faster while thinking), every memory as a node grouped by topic (shared keywords), synapse curves, faint links between memories sharing words, a pulse to each memory used for a reply, flash on new memories, hover tooltip, click card with Forget, search highlight, "teach Nova" box. Labels beside nodes when wide; hover-only in the narrow window; overlapping labels skipped
- [ ] Short-term: summarize old turns instead of dropping them

### Phase 6 — Remote access (owner: "let's add remote access from my phone")
- [x] Phone page (assistant/remote/): its own server, started by `assistant voice` only when set up and switched on. It binds only to the PC's Tailscale IPv4 (never 0.0.0.0, never the LAN) and re-checks every 10 s, since Tailscale is often not up yet at sign-in. Peers must be on the tailnet (100.64/10, fd7a:115c:a1e0::/48). Host must be this PC's Tailscale IP, MagicDNS name or hostname (DNS rebinding). Origin checked on login and the websocket; strict CSP; textContent only. Plain HTTP inside WireGuard: no `tailscale serve`, so no admin-console steps for the owner
- [x] Sign-in: password (scrypt, ≥10 chars) + TOTP (RFC 6238, own implementation, ±30 s, no replays), secrets in Credential Manager. Per-device random session token in an HttpOnly SameSite=Strict cookie, only its SHA-256 on disk (data/phone.json), 30 days. 5 wrong tries in 15 min lock sign-in for 15 min and Nova says so out loud; a new phone signing in is announced too; both are logged to nova.log. A new password signs every phone out
- [x] Phone chat = core Session with remote=True, sharing scheduler/watchers/memory/subtitles/voice/spotify (not grid/teacher/voicelock); remote_blocked_tools (+ dictation) refuse typing, clicking, files, power, routines, lessons. Confirmations as a Yes/No card on the phone with registry.describe text. Conversation per device survives reconnects (last 100 events replayed). Quick buttons are fixed tool calls (media, volume, lock_pc, stand down/wake, stop). Timers + watches with cancel, PC vitals, reminders forwarded (with vibration). Optional "read replies aloud" via the phone's own speech. Web app manifest for Add to Home Screen
- [x] Window: Phone page (set-up steps with Tailscale detection, QR as a data: URI from segno, first code confirms; addresses; on/off; new password; forget all; signed-in devices with Sign out). Phone requests show in Activity. `doctor` line
- [ ] Not yet tried on the PC or a real phone (checked with Playwright here at 390 px and 1180 px). No push notifications (needs HTTPS + a service worker); phone voice uses the keyboard's mic
- [x] Talk like at the desk (owner: "speak to Nova as if I was sitting here at my computer, does the same things, answers the same stuff"). Mic button on the phone (MediaRecorder, 60 s max) -> POST /api/voice (Origin + cookie, 8 MB cap) -> faster-whisper decode_audio (webm/opus and mp4/aac checked) -> the PC's own STT -> the same Session turn -> Kokoro reply as a WAV over the websocket (`audio` event), capped like spoken replies. Audio isn't kept. remote/voice.py
- [x] PC control from the phone, owner's pick "ask on the phone": `phone.pc_control: ask` turns remote_blocked_tools into CONFIRM (Yes/No card on the phone), `off` keeps the old blocks; REMOTE_NEVER (run_shell, voice_lock, teach, dictation, replay) stays blocked. The grid is shared now. "Work it out" from the phone asks first too
- [x] HTTPS for the mic: `tailscale cert` for the MagicDNS name into data/phone-cert, uvicorn serves TLS, address becomes https://name:port. If certificates aren't enabled, typing still works and the Phone page + phone say how to turn them on
- [x] Owner confirmed phone voice + PC control work ("it all worked flawlessly")
- [x] Nova's bubble on the phone (owner: "make it so you can see Nova's chat bubble thingy in the phone app"): the PC window's orb.js served at /orb.js; shows this conversation (listening with the phone mic's level while recording, thinking, speaking while the reply plays, amber for a Yes/No) else home standby/muted/offline; tap it to talk; talking cuts off a playing reply; big when the chat is empty, small in a chat, tiny while typing. Not yet seen on the phone
- [x] Security pass (owner: "make sure all security is tip-top shape"): REMOTE_NEVER blocked from the phone whatever remote_blocked_tools says; open_app/open_file/open_website ask on the phone; more Windows file types that run code or mount disks (.iso/.vhd/.msc/.appref-ms/.settingcontent-ms/.chm/.xll/.scf...) ask first and never open from the phone; Claude's hand-off has no file tools and no WebFetch (a web page can't talk it into sending documents away; prompt says page text is never instructions); phone sign-in body capped at 4 KB, voice uploads capped before reading and one at a time per phone; Secure cookie + HSTS on https, Permissions-Policy, COOP/CORP; dependency floors past known fixes (starlette, h11, pillow, urllib3, requests, protobuf, certifi). pip-audit: no known vulnerabilities. tests/test_hardening.py
- [x] Phone bugs (owner's screenshots): the reply showed Nova's whole <context> block (ContextFilter strips it from the stream, prompt says never repeat it); Nova said there was no "junk job" reminder while Up next showed it (the context now lists the timers and reminders set)
- [x] "He answers so randomly" (owner's screenshot: a question got his remembered facts read out, then a made-up "Opening business.facebook.com"): only memories the request is about reach the model (and at most 3); shortcut-shaped memories become taught shortcuts; every sentence is checked for made-up actions, not just the first; the prompt says answer what was asked and follow up on the last reply
- [x] "If I tell it to do something it does it": the prompt no longer invites "I can't"; every way of saying no to an instruction ("unfortunately", "I'm afraid", "beyond my capabilities", in any sentence) is nudged to use a tool, then handed to Claude
- [x] "It says I can't help you with that": the small model's refusals are held back; Claude answers the same request and Nova says it (the refusal stays only if Claude can't be reached); the prompt says help with anything the owner asks, no refusing or lectures
- [x] Nova sees the screen himself (owner: "instead of having to use Claude to see the screen ... real time access"): Windows' built-in OCR reads the window in front in a fraction of a second, locally; "what's on my screen" / "read my screen" / "what does it say" are answered by Nova from the text; "click X" clicks the words on screen when the app doesn't list its buttons (games, web pages) and looks again to check; Claude's hand-off reads text before taking screenshots; the picture goes to a vision model only for visual questions. Doctor line. Not yet tried on the PC (the Windows OCR call itself can't run here)
- [x] Volume "OSError [WinError -2147417850] Cannot change thread mode after it is set" (owner's screenshot): every COM user goes through core/com.py (same mode, tolerant of a thread that's already set up); unexpected tool errors are said in plain words, details to the log
- [x] Whole tasks (owner: "say complete the task on my screen and he will go through and complete the task until it's finished"): "complete the task on my screen", "fill out this survey", "finish the form", "do what's on my screen" go straight to Claude in long mode (30 min, 250 steps, page after page until a thank-you/done page; picks ordinary answers for questions about the owner; never passwords, payment, ID or contact details; stops on sign-in/CAPTCHA); Claude can now scroll and wait for pages; "stop" halts it; not saved as a shortcut. Not yet tried on the PC
- [x] Whole jobs worked out from the goal alone (owner: "complete this assignment for me ... switch to the homework tab, read all of the requirements and either do the quiz or ... open a notepad and answer all the questions ... he should be able to figure out that he has to do that on his own"): any "complete / do / finish / answer this <assignment, homework, worksheet, essay, questions, quiz, form...>"; Claude finds the right window or tab, reads every requirement, decides where the answers go (in the page, else Notepad, numbered and saved in Documents), does and checks every part, and leaves graded or outgoing work ready without the final Submit unless told. Not yet tried on the PC

### Phase 7 — Proactive + polish
- [ ] Morning briefing, hardware/disk/download alerts, background research tasks
- [x] Always-on (owner's pick): `assistant background` = a tiny supervisor that runs `pythonw -m assistant voice --background` and restarts it after a crash or a failed start (Ollama still loading at boot): 5 s, doubling, max 60 s, reset after a 10-minute run; tray Quit ends both, Restart starts fresh. Tray icon (pystray) shows the state in colour with Open / Microphone off / Stand down-Wake up / Start with Windows / Open log / Restart / Quit. `assistant autostart on|off|status` (HKCU Run key: no admin). No console: print output and errors go to data/logs/nova.log (1 MB x 3, colours stripped). One Nova at a time (named mutex): a second start opens the window. At sign-in: a soft chime, no speech, no window. update.ps1 stops a running Nova first (locked files) and starts it again after. Not yet tried on the PC
- [ ] Installer, offline fallback

### Polish pass (owner: "he doesn't cancel things, and he pauses and freezes")
- [x] Universal "cancel" / "never mind" / "cancel that" / "undo that" (VoiceLoop.cancel_last): stops Nova if busy, else hides the grid/numbers, else drops a lesson, else ends dictation, else undoes the last undoable action within 2 min (timer/reminder/alarm, restart/shutdown countdown, watch, subtitles, a memory, grid). Only when meant for Nova (the name, the follow-up window, Nova busy, or grid/dictation/lesson active), so "cancel" in game chat does nothing. "Cancel that" also works as a stop word now
- [x] No more silent freezes: every tool has a time limit (30 s default; long ones listed in registry.TOOL_TIMEOUTS) and reports "took too long"; Ollama timeout 60 -> 30 s; if nothing has been said 1.8 s after a request, "One moment, sir."; a soft tick the moment a request is accepted (voice.ack_sound)
- [x] Plain "pause/resume/skip/previous" act on whatever is actually playing (media sessions: Spotify, YouTube, any player; media key fallback) instead of always Spotify
- [x] "No, I meant X" / "I said X" is handled as X
- [x] Owner: "when I tell him to full screen my video or things like that he doesn't". Causes: Windows blocks background programs from switching windows, so F went to whichever window was in front; and F only works when the page (not YouTube's search box) has the keyboard; and Nova reported success without checking. Now: WindowBackend.focus (Alt tap + AttachThreadInput + minimise/restore fallback, verified); video full screen clicks the player's own "Full screen" button (UI Automation) with F as backup and checks the window really covers the screen (says so if not); mute clicks "Mute"; "switch to X" reports when Windows refuses; typing/keys first return the keyboard to your window if Nova's window is in front
- [x] Grid/numbers instructions spoken once per session, then just "Grid on." / "Numbers on."

### Polish pass 5 (owner: "go ahead and go through and polish up everything you can")
- [x] Windows' media list stuck on the owner's PC: not asked again for a minute after it hangs (every media command waited 4 s); "play X" trusts Spotify's own check instead of 8 slow Windows checks (was 30 s+, then a false failure)
- [x] Minimise / maximise / restore read the window state back; volume and mute read back from Windows
- [x] Claim guard split: "Skipped...", "I've closed..." always count; "is open" / "now on" only when an action was asked or the owner complained (news like "the sale is now on" was flagged)
- [x] Everyday phrase audit re-run: 93/106 direct, no regressions
- [ ] Not yet tried on the PC

### Made-up claims (owner: "he gets more and more stupid the more you do, can you just actually fix the problems")
- [x] "Skipped." with nothing skipped: Windows' media list empty -> Spotify checked before/after, else "pressed the key, can't check"
- [x] "Firefox is reopened" / "Now on YouTube" / "Closed Spotify" with no tool run: any unbacked action claim is held back (Claude or an honest reply)
- [x] "Close Spotify" closed Firefox: the model can't close "this" unless the user said this/it; Spotify only in the tray is quit (checked)
- [x] "You didn't skip anything", "You didn't pause it. Just go ahead and close Spotify.", "...you still haven't closed Spotify": done directly, no model
- [x] "Take me back to YouTube": its tab, else opened; "go (back) to Spotify": the app; leaked ">window close" text removed
- [ ] Not yet tried on the PC

### Work it out + learn (owner: "it keeps saying I can't do this and that ... allow it to self learn and be able to work through things"; chose automatic)
- [x] agent/tools_server.py: MCP bridge (own JSON-RPC, no new dependency) lending Nova's SAFE tools + screenshot + screen_click; nothing that asks first, no phone, terminal typing refused, sitecheck applies
- [x] brain/agent.py: Claude Code with that bridge only (+ WebSearch/WebFetch), DONE:/FAILED: verdict, steps streamed to the live feed
- [x] Automatic: failed PC actions and "I can't" to action requests are held back and handed over; "figure it out" / "try another way" asks directly; deliberate refusals (banned site, declined) never are
- [x] Learning: the steps that worked become a lesson; next time instant with no Claude
- [x] Video play/pause through the player's own button when Windows' media list is stuck (owner: "it can't play the video")
- [ ] Not yet tried on the PC

### Scam lookalike (owner: "make it fact check what the real url is before it goes to it, it just opened this scam website like 50 times")
- [x] sitecheck: lookalikes -> real site with a note; banned sites never open (block/unblock/list by voice); unknown names searched, not guessed; in all three website paths
- [x] "Stop taking me to that website" / "don't ... again": no model, no tool; bans the site just opened
- [x] "Open a new tab for me? Can you open Kelly Blue Book?" -> one new tab with kbb.com (sentences split; "for me" isn't a place); "open the website X"; "Nova" alone -> "Yes, sir?"
- [ ] Not yet tried on the PC

### Wake word + browser slips (owner: "he answers everything I say when I don't say his name"; "I asked it to close a tab and it closed the browser ... now the hud is not moving")
- [x] Follow-up window, talking over Nova and pause-and-continue no longer bypass the name for chat (see CLAUDE.md lesson); stand down needs the name (the "frozen" window was stand-down mode, triggered by "stand down!" from GTA RP)
- [x] "Close Gmail" closes the Gmail tab (was: the whole of Firefox, matched by window title); switch to a tab name switches tabs; no browser open -> new tab/go/reopen start the default browser
- [x] "type in X" types X; "press the Instagram" clicks it; app names capitalised
- [ ] Not yet tried on the PC

### Your browser and any app (owner: "he still struggles doing simple tasks like open a new tab on my browser, I want him to control and navigate my browser and any app effortlessly")
- [x] Causes: "open a new tab on my browser" wasn't understood (went to the model); shortcuts went to whichever window was in front (Discord, the game, Nova's window)
- [x] `my_browser` (tools/mybrowser.py): finds the user's browser (front, else last used), brings it forward (checked), then new tab (+ site/search), go, switch/close tab by name or number, next/previous, reopen, back/forward, reload, list tabs, find on page, click a link. Verified by the tab strip (UIA TabItems, top row/left column only), window title, address bar. Without a tab list: Ctrl+Tab through the tabs reading titles. press_keys with a browser in front takes the same path
- [x] `app` (tools/apps.py): look (numbered buttons/boxes/tabs/menu items), click/double/right click by name or number, type into a named box (+Enter), press keys, in the named app brought forward first; terminal guard kept
- [x] Keys carry scan codes and are held briefly; both tools blocked from the phone; model sees 31 tools (single-step click/type/keys hidden, the fast path uses them)
- [ ] Not yet tried on the PC (Firefox's tab list through UI Automation is the main unknown; the Ctrl+Tab fallback covers it)

### Polish pass 4 (owner: "continue polishing, I am not home to test")
- [x] Found why Nova "bugs out after 5 minutes": measured with Qwen's own tokenizer, the system prompt + tools are ~5,200 of the model's 8,192 tokens, and every turn re-sent 20 turns of history with their context blocks and full tool results. Past the limit Ollama silently drops the oldest messages, mid tool loop even the request. `brain/fit.py` now decides what's left out (stale context blocks, then old tool results, then old turns, then this turn's older tool results) and always keeps the request; 8-13 recent turns still fit
- [x] Second phrase audit (~100 phrases): 35 went to the model, now 12 (jokes, news, and things Nova can't do yet like clips or Wi-Fi). Fixed: "play X on YouTube" went to Spotify, "click on search" looked for "on search", "type my email" typed those words, "open youtube and search for X" was split in two
- [x] New direct commands: louder/quieter, RAM, time in another city, reminders/alarms on a day, skip the ad (checked), skip/rewind by time, days until, empty the recycle bin (confirm, not from the phone, checked), who are you / what can you do, summarise this page (Claude), what's on my screen, repeat this song (checked), brightness (checked), unit and currency conversion, go back to the game / switch back, quit tray apps
- [x] Checked instead of claimed: open_app waits for the window ("starting" otherwise; finds Steam .url games, desktop shortcuts, Store apps; "play GTA" opens the game), close says when an app asks to save or stays in the tray, Spotify shuffle/repeat read back
- [x] The prompt no longer names tools hidden from the model (it had to guess their arguments); a test guards it
- [ ] Not yet tried on the PC

### More brains (owner: "he says I opened a new tab but he did nothing, he doesn't know when he's selected onto the browser to use the hotkeys") + "I, Oliver, made him and he obeys me"
- [x] Every model turn now gets the situation in its context (brain/situation.py): the window in front (the one keys and typing go to), what Nova's own browser shows, and what's playing
- [x] Keys go to the owner's window (never the Nova window) and the reply names it ("Opened a new tab in Firefox."); tab/back/forward/reload shortcuts are checked by the window title changing, otherwise Nova says "I pressed ctrl+t in Firefox, but nothing changed" instead of claiming it
- [x] New tab / close tab / next tab / back / forward / reload while Nova's own browser is in front are done by the browser itself (Playwright), not by blind key presses
- [x] `assistant.owner_name` (Oliver): the prompt says Oliver made Nova and is in charge (act straight away, don't question or lecture); "who made you" / "who's your boss" answered instantly. The confirmations for risky actions (delete, shut down) and the phone blocks stay: they are the system's, not the model's
- [ ] Not yet tried on the PC

### Fix: "he can't even take simple requests like stand down, he's straight up ignoring me"
- [x] Likely cause: voice lock is on (every screenshot shows the pill) and was learned on the old Fifine mic; with the Logitech headset the owner's voice can score under the bar and everything was dropped silently (only visible with "show ignored speech")
- [x] Stand down / stop / cancel always pass the voice lock (safety words)
- [x] A refused voice within 0.15 of the bar: Nova says "Sorry, I didn't recognise your voice" (at most every 90 s), the Home live feed shows it with a "That was me" button, and Activity always lists it. Far-off voices (game chat) stay silent
- [x] "That was me" (window only, so strangers can't use it): adds that recording's voiceprint (numbers only; e.g. the new headset) and does what was asked (`VoiceLoop.accept_rejected`, `VoiceLock.adopt`)

### Fix: "you broke him" (screenshots: "I cannot open Edge or any browser", "I cannot directly navigate to kbb.com", "Pause the video and open spotify" -> "Sorry, I wasn't able to do that")
- [x] Root cause: every feature added tools; at 54 the 4B model stopped calling them and claimed or refused instead. `tools.MODEL_HIDDEN` hides 23 tools that are said directly or rarely needed (still run by the fast path, routines, the window): the model now picks from 31
- [x] Compound commands split into direct commands when every part is known ("pause the video and open spotify", "open steam and discord" reusing the verb, up to 3 parts); single-intent phrases ("open kbb.com and search for X", "full screen the video and play it") stay whole
- [x] False "I can't" (open/browse/navigate/access...) counts like a false claim: never read out for action requests, the model is told it CAN (browser tool) and retries. The prompt lists what Nova can do
- [x] "Open Edge and look" searches what was just looked up (web_search / site search / browser search) in Nova's browser
- [x] Browser phrases: "go to kbb.com and look at the resale value of X", "what's X worth on kelley blue book", "look up X on amazon", "check kbb for X"; the query drops "the resale value of" etc.; only known sites or addresses count
- [x] Offers at the end of replies ("Would you like me to...", "Anything else?") aren't read out
- [x] tests/test_owner_cases.py reproduces the three screenshots
- [x] Owner: "you didn't close my video and you didn't open Spotify!" got "Sorry, I wasn't able to do that" (and the model had *played* the video). "Close/exit/kill the video" now pauses it (fast path); "you didn't X (and you didn't Y)" does X and Y directly when they're known commands

### Polish: simple commands (owner: "Nova is still struggling to answer me on simple commands")
- [x] Audited ~100 everyday phrases through the routing. Now done directly (no model): the date/day, "remind me in 20 minutes to X" / "remind me to X at 6 pm", alarms ("set an alarm for 7 am", "wake me up at 7:30"), shut down / restart / sleep the PC (still asks first), "how's my PC doing" and CPU/GPU/battery/memory/disk, "open my downloads (folder)", File Explorer (was blocked by the "file" filter), "search for X" / "google X" (opens the results), exact sums (tools/quick.py `calculate`: safe arithmetic, percent, square root), the weather (Open-Meteo, free and keyless; "my city is Leeds" saved in data/location.json), and instant replies to thanks/greetings/"how are you". 80 of the 100 phrases are direct now; the rest are chat and knowledge for the model. tests/test_everyday.py is the regression table
- [ ] Not yet tried on the PC

### Nova's own browser (owner: "open kelley blue book kbb.com and search for a car, and it can effortlessly navigate the browser")
- [x] tools/browser.py: Playwright drives the installed Microsoft Edge (channel msedge, no browser download) with its own persistent profile (data/nova-browser), so the owner's browser and logins are untouched and no mouse/keyboard/focus is needed. Actions: open (names like "kelley blue book" -> kbb.com, an address inside a phrase, unknown names -> DuckDuckGo !ducky), search (finds the page's search box incl. behind a magnifier button; types + Enter; checks the page changed or shows the words; else the site's best page via !ducky site:), click / type / select (by number from the returned list, or by label/text; dropdown choices checked first), press, scroll, back, read (page text, marked information only), look. Each step returns the title + up to 30 numbered elements (data-nova-id) so the model can continue; the prompt tells it to keep going until done. New tabs are followed; a closed window reopens
- [x] "Open a website" goes to Nova's browser (`browser.handle_websites`, default on) so follow-ups work; YouTube/Google searches stay as before. Fast path: "open kbb.com and search for a 2019 honda civic", "search kbb for X", "on ebay search for X", "search for X on kbb"; while Nova's browser is in use: "search for X"; while it's in front: "click X", "scroll down", "go back" ("click the first result" goes to the model)
- [x] Remote-blocked from the phone; 60 s tool limit; closed on quit; live-feed icons; doctor line
- [x] Owner's test: Edge showed a "--no-sandbox" warning bar and kbb.com answered "Access Denied" (bot wall) to the automated window. Now it launches like a normal Edge (sandbox on, no --enable-automation, AutomationControlled off, navigator.webdriver hidden); a bot wall is recognised, reported honestly, and the page is opened in the normal browser. Clicks through covering banners (element click); the search-box opener only clicks real search buttons
- [x] Tested for real with a headless Chromium against local test sites (search, click by number, read, back, dropdowns, missing search box, reopened window). Not yet tried with Edge on the PC or on kbb.com itself (no internet to it here)

### Fix: "it's barely responding, I tell it to do something and it doesn't do it"
- [x] Bad lessons: "Sorry, can you open Steam" right after "open Discord" was learned as a correction, so "open Discord" then opened Steam. Now "sorry/actually/oops ..." aren't corrections, a bare "no, X" is done but not learned (only "I meant/it should be/wrong one/that's not what I asked" teach), correcting a replayed lesson deletes it, and lessons start fresh (data/lessons-v2.json)
- [x] "Fire up Discord" -> "Discord is now open" with no tool: "fire up/get/load/run/..." now count as action requests, so the claim is nudged and the tool runs
- [x] Never silent: an empty model reply becomes "Done." (it acted), "That didn't work." or "I didn't catch that"

### Self-learning (owner: "if I tell him to open a website and he does it wrong I want to be able to correct him, like a self learning AI")
- [x] Corrections (brain/lessons.py, a layer in front of `LocalBrain.run_turn`): within 5 min of an action, "no, I meant X" / "wrong one, X" / "you opened the wrong app, I wanted X" does X (a fragment goes to the model with what it corrects), and when X's tools succeed, the original phrase -> those exact tool calls is saved (data/lessons.json). Next time the phrase (politeness ignored, near misses at 0.9) replays the calls with no model. "That's wrong" alone -> "What should I have done?", the next answer is the fix ("never mind" drops it). "No thanks", "no way..." aren't corrections. Look-ups (time, search, screen...) are never learned
- [x] Teaching: "when I say X, Y" -> X means Y from now on
- [x] Preferences said in passing ("I prefer ...", "my favourite ...", "I usually ...", "from now on always ...", "call me ...") go to memory without "remember" (not questions, not "I like this song"); secret refusal still applies
- [x] "What have you learned" (lessons + routines shown to Nova), "unlearn that", "forget what you learned about X". Brain page: "Learned from you" list with ✕; dashboard memory card counts lessons and pulses when one is learned
- [x] Fast path: "open bbc.co.uk" (any web address) opens the website, not an app called "bbc co uk"
- [ ] Not yet tried on the PC

### HUD overhaul 2: the dashboard (owner: "make his bubble better looking, show what he's doing on the dashboard, kind of controlled by Nova")
- [x] Home (the new first page): a glass dashboard. Nova's bubble is now a particle sphere (900 points, Fibonacci sphere, perspective, additive glow) that breathes with the mic level, ripples while speaking, swirls while thinking, inside two orbiting rings with a travelling node; the colour follows the state (orb.js; one simulation drives both canvases). Captions under it: what you said, what Nova is saying. Stop / mic / stand down, and a type box
- [x] Live: a card per action as it happens, in plain words from the tool's arguments ("Opening Discord", "Playing “my way” on Spotify", "Setting a 10 min timer"), coloured icon, spinner, then ✓/✗ with the result and time. The top bar shows the current action on every page. Tool events now carry short arguments (`pipeline._brief_args`)
- [x] Cards Nova lights up (pulse) when it touches them: Now playing (Windows' media list every 2 s through its own reader, with prev / play-pause / next buttons that use the exact `media` tool), Up next, This PC (ring gauges), Remembers (count + latest)
- [x] Controlled by Nova: `show_page` tool + fast path ("show me my timers", "open your brain", "back to the dashboard") sends a navigate event; the window follows. The Yes/No card floats over every page
- [x] Checked with Playwright at 1180, 1440 and 430 px; not yet seen on the PC

### Polish pass 3 (owner: "he keeps saying I'm playing this on Spotify but nothing is happening, and that he played and full screened the video but isn't doing anything")
- [x] Spotify: success only when this PC really plays it. Spotify's servers said "playing" about the old song or a stale state; now the item/context must be what was asked (relinked copies by name), searches use market=from_token and skip is_playable:false, the transfer retry uses play:true. Then Windows' media list (GSMTC) must show Spotify playing that title. If not: open the URI in the Spotify app and click its own Play button (UIA: "Play <title>..." row, else the topmost plain "Play" above the player bar), check again, else say so. `spotify test [song]` prints what Nova, Spotify and Windows each report
- [x] Owner (Premium): "Spotify refused that. Playback control needs Spotify Premium." Every 403 used to be reported as "no Premium". Now Spotify's actual reason is kept and logged, and a refused play goes to the in-app route (open the song in the app, press its Play button, check Windows). `spotify test` also prints the account type the API sees
- [x] Video: play/pause are re-read from Windows' list after the command; if nothing changed, the player's own Play/Pause button (or k/space) is used, and otherwise an honest error. Plain pause/resume (`media`) likewise, with the media key as backup
- [x] Full screen: a maximised browser on a screen with no taskbar (the owner's second monitor) covered the monitor and counted as full screen, so Nova said "already full screen". Now full screen also needs no title bar and no resize border (`pc.fullscreen_style`)
- [x] Model claims: "I played ...", "I opened ...", "Playing X", "X is now playing" count as claims; "put on/make/go" count as action requests. For action requests, the model's words are held until it's clear whether it used a tool, so a false "done" is never spoken (nudged instead; "Sorry, I wasn't able to do that" if it still doesn't act)
- [x] Fast path: "play it"/"play" resume (was a Spotify search for "it"); "put on X"; "play something by X"
- [ ] Not yet tried on the PC

### Polish pass 2 (owner: "he doesn't really allow me to interrupt anymore and he talks a ton instead of just doing")
- [x] Interrupting with voice lock on: speech heard over Nova's own voice matched the voiceprint worse and was dropped as "not your voice". Now it gets 0.12 extra leeway (`voiceprint.OVER_SPEECH_LEEWAY`); "stop"/the name while Nova is busy always works (harmless); the voice check runs alongside Whisper instead of after it; the first barge-in check comes after 0.45 s instead of 0.8 s; 2 new words are enough to interrupt (was 3; the echo check still catches 2 of Nova's own words)
- [x] Less talk: out loud, replies stop after 3 sentences (10 when asked to explain/tell about/list...), then "The rest is in the window."; the full text is still shown. A round that starts like a preamble ("Certainly, sir. I'll ...") is held back and dropped when it calls a tool. Prompt: no words before a tool call, confirmations in five words, act instead of asking. get_time says "It's 1:26 PM, Sunday 27 September."
- [x] Doing, without the model: time, volume up/down/set/mute, open/launch/start an app (known sites open in the browser), close/quit an app or "this", switch to/bring up an app, minimise/maximise/restore this. Multi-part requests, files and routines still go to the model
- [ ] Not yet tried on the PC

### HUD overhaul (owner: "make it look a lot better, it's poorly made now")
- [x] Command-centre layout: nav rail with icons and badges, page topbar with pills, right-hand core panel (orb, controls, confirm card, Up next, This PC vitals: CPU, RAM, GPU temp, VRAM every 3 s). Chat: welcome screen with a time-of-day greeting and clickable suggestions, avatar bubbles, tool chips under replies instead of system lines. Timer cards with big countdowns and progress bars (amber in the last minute); routine cards with icons; Voice page as two cards. Background tint follows Nova's state. Responsive (bottom nav under 860 px). Window now opens at 1180×760. Checked with Playwright screenshots at 1180 and 440 px here; not yet seen on the PC

### Voice lock (owner's pick from the menu)
- [x] voice/voiceprint.py: Resemblyzer's GE2E voice encoder (40 mels -> 3-layer LSTM 256 -> 256-d, L2) reimplemented in numpy. Weights come from the Resemblyzer 0.1.4 wheel on PyPI (SHA-256 pinned), read with a torch-free reader for the legacy torch.save format, stored as models/voice_encoder.npz (5.7 MB; `assistant models` fetches it). Checked here against PyTorch + librosa: identical weights, mel rel. error < 2e-7, embedding cosine 1.0000; ~90 ms for 3 s of speech on CPU
- [x] "Nova, learn my voice": read 5 lines (no name needed; a sample must be the asked line and not heard over Nova's own voice); threshold calibrated from how alike the 5 are (leave-one-out cosine - 0.06, clamped 0.62-0.82); up to 5 prints (e.g. one per mic: "learn again"); only numbers stored (data/voiceprint.json), no audio
- [x] Enforcement: every utterance is checked in parallel with STT (short phrases get 0.05 leeway); strangers are ignored (Activity shows the match score), including yes/no answers, cancel, stand down and barge-in. Typed HUD text isn't checked. HUD Voice tab: lock card (learn / on-off / forget / strictness slider / last match %), 🔒 badge; tray/orb colour while learning; doctor line. Real-voice separation not yet measured on the owner's PC (espeak voices here are all too alike to judge)

### Phase 8 — Unique features (owner picked 3 of my ideas)
- [x] "Tell me when ..." watchers (core/watchers.py, tools/watch.py): browser downloads (partial-file extensions in Downloads), Steam downloads (steamapps/downloading of every library), an app closing or going quiet after working (CPU), GPU above/below °C, CPU calming down, battery full/low (not while plugged in), internet back, a web page changing (text fingerprint with digits ignored; "this page" = the active browser's address bar via UI Automation). Announced once like reminders (chime, held while standing down); max 10, expire after a day; listed with cancel buttons in the HUD's Timers tab. Fast path needs "tell me/let me know when ..." so questions like "when is dinner done?" aren't watches. Not yet tried on the PC
- [x] Teach by showing (tools/teach.py; owner's pick from the second idea list): "watch what I do" -> "done" -> a name -> a routine in data/learned.json. Recording polls GetAsyncKeyState ~100x/s in a thread (no hooks: the keyboard hook once glitched the owner's keyboard). Each click stores the app (process/title), the named element under it (UI Automation ElementFromPoint, walking up from icons to their button), the spot relative to the window, and the screen spot; double clicks and drags detected; typing grouped; shortcuts separate; gaps become waits (max 3 s). Password boxes (UIA IsPassword) are never recorded; clicks on the Nova window ignored; Nova's own repeatable actions during the lesson (open_app, open_website, window, volume, video, music...) recorded via registry observers. Replay: replay_click finds the app's window (waits up to 8 s for it to open), focuses it, finds the button by name (waits up to 4 s; nearest to the recorded spot if several), else the relative spot; replay_keys goes through the terminal guard. Learned routines appear live in the HUD Routines tab (✕ to forget); the orb is red while watching. Windows parts faked in tests; not yet tried on the PC
- [x] Live subtitles + translation (voice/subtitles.py): WASAPI loopback of the default speakers via `soundcard` (never the mic) -> 16 kHz mono -> its own Silero VAD -> segments (final after 0.6 s quiet or 8 s; partials every 1.5 s) -> Whisper with language detection (`transcribe_any`, same loaded model, no hotwords) -> if not English, the local model translates with the chat model's exact options (a different num_ctx would make Ollama reload it). Caption box: a Tk Toplevel on the grid overlay's thread, bottom centre, see-through and click-through; original small above, English big below; partials in italics; hides after 6 s of quiet. Nova's own voice is skipped; Whisper's music hallucinations ("Thank you.", "♪") filtered; captions never logged. "Subtitles on/off", "translate this", "what are they saying", CC button in the window. Loopback capture can't run here; tested with a fake capture; not yet tried on the PC
- [x] Voice designer (voice/voicedesign.py): blend up to 3 Kokoro voices (weighted average of their style arrays), speed, pitch (-4..+4 semitones: Kokoro renders at speed/f, then the audio is resampled by f, so the pitch moves and the talking speed doesn't), British/American pronunciation. Saved to data/voice.json (wins over config.yaml). HUD Voice tab: presets (Butler, Deep butler, Young gent, Lady Nova, Nova (American), Movie trailer), mix rows, sliders, Preview (plays without switching), Save (switches live, clears the phrase cache, re-warms). "Change your voice to deep butler" / "use your normal voice". Kokoro can't run here: tested with a fake Kokoro; not yet heard on the PC

## Open questions for the owner
- Happy with the hotkeys above? (assumed yes until told otherwise)

### Kokoro GPU rewrite: acceptance
- Rebuild after update: STFT maths 6.8e-7, loudness x1.00, length x1.00, but waveform difference 0.32 → rejected. Cause: Kokoro takes atan(imag/real) of the STFT. Where real ≈ 0, rounding decides its sign (a π phase flip), so no two STFT implementations can match sample-for-sample (the reference is just ORT's rounding)
- Acceptance is now: exact STFT maths, same length, loudness within 5%, and a log-spectrogram distance to the reference no worse than max(1 dB, 1.5× the original model's own distance). `models --debug-stft` compares against the reference and shows the values at the worst positions
