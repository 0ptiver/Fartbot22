# CLAUDE.md — handoff notes for Claude

Read this first. Then read PLAN.md (phase checklist + history) and README.md (user-facing docs).

## What this is
**Nova**: a voice-first personal assistant (JARVIS-style) for the owner's Windows 11 gaming laptop.
British-butler personality, calls the owner "sir". The code package is `assistant/`, so renaming is config-only.
Branch: `claude/jarvis-voice-assistant-lhnfza` (commit and push there after every milestone).

## The owner
- Not a developer. Explain things simply, give copy-paste PowerShell commands, and ask for screenshots.
- They test on the real PC and send screenshots. **I can't run anything on their hardware.**
- Hardware: i9-14900HX, RTX 5070 Laptop 8 GB (Blackwell), 32 GB RAM, **two screens**. Mic: was a Fifine SC3 (`input_device: 3` in their `config/local.yaml`); the latest doctor output showed a **Logitech PRO X Wireless** headset. Output: Realtek speakers, so echo matters. Computer name `LAPTOP-VKHGAOUV`.
- Plays GTA RP (FiveM) with voice chat running, so there's lots of other speech in the room. Watches YouTube in **Firefox**. Uses Steam and Discord.
- Has Claude Pro, Ollama, Spotify Premium, and Claude Code installed and signed in. Budget: free or subscription only, no API fees.
- Cares a lot about **security/privacy**. Likes "unique" features and a polished feel. Complains quickly (and fairly) when Nova says it did something it didn't.
- Tell them to update with `powershell -ExecutionPolicy Bypass -File scripts\update.ps1`. **Never tell them to run `pip install` directly**: it reinstalls CPU onnxruntime over onnxruntime-gpu and the voice drops to ~650 ms. update.ps1 also stops a running tray Nova first (Windows locks its files) and restarts it afterwards.
- Nova usually runs in the **tray** now (autostart on). Its console output is in `data\logs\nova.log`; ask for that file when something goes wrong. Foreground run: `.venv\Scripts\python -m assistant voice` (quit the tray one first: one instance only).

## My sandbox (Linux cloud container)
- Blocked: huggingface.co, github release downloads, ollama.com, developer.spotify.com. PyPI works. There's no GPU, audio device, Windows or Ollama.
- So real models and Win32 can't run here. Every Windows part sits behind a swappable backend (pc.WINDOWS, pc.POWER, grid MouseBackend/TkOverlay, keyboard.KEYBOARD, uia.UIA, video.MEDIA, teach InputPoller) and is tested with fakes.
- `.venv` (Python 3.12, `uv`). Run `timeout 100 .venv/bin/python -m pytest -q` (~490 tests, ~18 s) and `.venv/bin/python -m pyflakes assistant tests`.
- **Beware unbounded loops in tests**: fixtures set delays to 0, and a while loop keyed on elapsed time hangs pytest. Always use `timeout 100`.
- `pkill -f <pattern>` kills your own shell if the pattern matches the command line (exit 144). Let background demo servers time out instead.
- conftest autouse fixtures redirect `voicedesign.VOICE_FILE` and `teach.LEARNED_FILE` to tmp; `settings.memory.path` is tmp too. Keep file paths looked up at call time (not as default args) so tests can redirect them.
- Visual checks: HUD screenshots with Playwright from a scratch venv (`pw/`) using `executable_path="/opt/pw-browsers/chromium"`; Tk overlays under `Xvfb :99` with `/usr/bin/python3.12` (has tkinter) in a scratch venv with `--system-site-packages` (`tkv/`). Both live in the scratchpad, not the repo.
- Checking Windows-only wheels: `pip download --only-binary=:all: --platform win_amd64 --python-version 3.12 <pkg>` and inspect the .whl.

## Architecture (assistant/)
- **Request flow:** VoiceLoop (voice) or HUD typed text → `LocalBrain.run_turn` → fast path first (no model): teach state (`teach_intent` with the Teacher) → `match_routine` → `intents.match_intent`, whose order is memory → watch → teach → voice/subtitles → timer → grid/numbers → video → keyboard → music rules → "play X". Anything else goes to the model with tools. `_CORRECTION` turns "no, I meant X" into X.
- **Tools** get a `ToolContext` whose `services` hold the live objects: `brain`, `voice` (the VoiceLoop), `scheduler`, `watchers`, `grid`, `subtitles`, `teacher`, `memory`, `spotify`.
- `core/`: config (`config/config.yaml` defaults + git-ignored `config/local.yaml`, deep-merged), secrets (env/.env then Windows Credential Manager), FastAPI server (`/ws`, 127.0.0.1 only, token + Origin/Host checks), scheduler (timers/reminders, `data/reminders.json`), `paths.py` (folder allowlist), `memory.py` (SQLite+FTS5, secret refusal, same-subject replace, listeners), `watchers.py` ("tell me when"), `launch.py` (open apps detached from Nova's console).
- `brain/`
  - `local.py` `LocalBrain`: everyday model in Ollama (`qwen3:4b-instruct-2507-q4_K_M`; the **instruct** variant, because the thinking variants read their reasoning aloud). ThinkFilter; warm-up with identical options; a nudge when the model promises **or claims** an action without calling a tool (the retry is held back, then replaced by "I wasn't able to do that" if it still doesn't act); a direct "ask Claude…" route; relevant memories added to the turn context.
  - `intents.py`: the regex fast path (see Request flow). `prompts.py`: system prompt + per-turn `<context>`.
  - `expert.py`: hard tasks via the **official Claude Code CLI on the owner's subscription** (`claude -p`, task on stdin, API keys stripped, `--permission-mode dontAsk`, WebSearch/WebFetch/Read with Read confined to an empty workspace, `--strict-mcp-config`). Never `--bare`: it ignores subscription login. Vision ("what's on my screen") goes here too.
  - `llm.py` `Brain`: Claude API backend (optional, paid).
- `tools/`: `registry.py` (JSON schema; risk safe/confirm/blocked; audit `data/audit.jsonl`; remote-blocked list; per-tool time limits `TOOL_TIMEOUTS`; `observers`; `internal` tools hidden from the model; fills in missing `properties`), system (time/volume/open_app), files (allowlisted folders, Recycle Bin), timers, pc (status, lock, power(confirm), window incl. robust `focus()`/`is_fullscreen()`, websites, press_key), grid (voice mouse + numbers overlay; Tk overlay thread; also draws the subtitle box), uia (click by name, show numbers, `current_url`), keyboard (type/keys, terminal guard, dictation tool), video (`video` = browser video via GSMTC + clicking the player's own buttons, checked; `media` = pause/play/next on whatever is playing), watch, memory, voice (voice presets + subtitles tool), teach (teach by showing + replay), routines (config + learned, registered last), music (Spotify + media keys), screen, expert, web.
- `voice/`: VoiceLoop (`pipeline.py`): Silero VAD endpointer + speculative STT; faster-whisper large-v3-turbo on cuda (`transcribe_any` for subtitles); Kokoro TTS on the GPU (`models/kokoro-v1.0.gpu.onnx`, STFT rewritten as a Conv by `tts/onnx_fix.py`, accepted by spectral distance; ~72 ms on GPU); wake by name (transcript-based, no hotkey) with a 6 s follow-up window that doesn't chain; verified barge-in, stop words, pause-and-continue merge; WebRTC AEC3 (`aec.py`); spoken confirmations; stand down/wake up; announcements with a chime; dictation; grid commands without the name while it's showing; **universal cancel** (`cancel_last`: stop → hide grid → drop lesson → end dictation → undo the last action within 2 min); a "heard you" `tick()`; "One moment, sir." after 1.8 s of silence. Also `subtitles.py`, `voicedesign.py` (voice mix/speed/pitch, `data/voice.json`), `speechtext.py`, `textnorm.py`, `wake.py`, `commands.py` (yes/no, stand down, cancel).
- `voice/voiceprint.py`: voice lock (numpy GE2E encoder from the Resemblyzer PyPI wheel, torch-free legacy .pt reader, VoiceLock enrol/check/calibrate, data/voiceprint.json). VoiceLoop.lock; checks run in parallel with STT in `_utterance_done`, enforced in `_handle_utterance` and `_barge_verify`; `_enrol_sample`.
- `hud/`: the window, served from inside `assistant voice` on 127.0.0.1:8766 (per-run key in the URL fragment, Host/Origin checks, strict CSP, textContent only) and shown in an Edge `--app` window with its own profile. Layout (overhauled): left nav rail (Chat, Activity, Timers, Routines, Brain, Voice), centre page with a topbar (title, voice-lock pill, Subtitles, Wake word/Open mic), right "core" panel (orb, Stop/Stand down/Mic, confirm card, Up next, This PC vitals); under 860 px the rail moves to the bottom. SVG icon sprite in index.html, `--glow` CSS var follows Nova's state. `static/`: `hud.js` (orb, chat with tool chips, activity, timer cards, routines incl. taught ones, confirm card, vitals), `brain.js` (memory brain view), `voice.js` (Voice tab). `server.py` pushes status 10×/s, timers/routines once a second, vitals every 3 s (psutil + cached nvidia-smi). `test_every_element_the_scripts_use_is_on_the_page` guards the ids the scripts use. Window opens at 1180×760.
- `remote/`: phone access. `auth.py` (PhoneAuth: scrypt password + own TOTP, secrets in keyring as PHONE_AUTH, per-device token hashes + on/off in data/phone.json, lockout), `server.py` (PhoneService binds to the Tailscale IP only and follows Tailscale up/down; `create_phone_app`: tailnet-peer + own-names Host checks, /api/login|logout|me, /ws with PhoneChat = core `Session(remote=True)` per device; fixed quick ACTIONS), `static/` (phone.html/js/css, manifest). The window's Phone page is `hud/static/phonesetup.js` + `phone_*` messages in hud/server.py. conftest redirects auth's files and turns keyring off.
- `background.py` (supervisor `assistant background`, autostart via HKCU Run, log file, single-instance mutex) + `tray.py` (pystray icon in the state colour).
- `integrations/spotify.py`: PKCE login, tokens in keyring. **Playback is parked** (see PLAN.md).
- CLI: `python -m assistant {voice [--background|--no-hud]|background|autostart on|off|status|hud|chat|serve|doctor [--full]|models|ttsbench|mictest|devices|spotify ...|tools|routines|audit|secrets set NAME}`.
- Data on the PC (git-ignored `data/`): audit.jsonl, reminders.json, memory.db, voice.json, learned.json, logs/, hud-url.txt, hud-browser/ (the window's browser profile).

## Lessons learned (don't repeat these)
- **Ollama rejects the whole tool list if any one schema is off** (e.g. an object without `properties`) and Nova can't start. `test_every_tool_schema_is_one_ollama_accepts` guards this.
- **Different Ollama options (num_ctx etc.) reload the model** (seconds). Anything else calling Ollama (warm-up, subtitle translation) must copy the chat model's exact options.
- **Windows won't let a background program switch windows.** Use `pc.WINDOWS.focus()` (Alt tap, AttachThreadInput, minimise/restore) and check its result. Never press keys blind, and never report success without checking (the owner notices).
- **Keys only reach a web page if the page has the keyboard.** Prefer clicking the app's own button, found by name via UI Automation, with keys as a backup.
- **No keyboard hooks**: the `keyboard` lib's hook glitched the owner's keyboard. Teach-by-showing polls `GetAsyncKeyState` instead.
- Apps started from Nova's console printed their logs into it (Discord): always open things via `core/launch.py` (detached).
- winrt packages don't pull in the namespaces their results use: list `Foundation.Collections`, `Media` and `Storage.Streams` explicitly (not `[all]`, which pulls ~85 packages).
- The 4B local model fumbles multi-step PC control: put common commands on the fast path, keep spoken results short, and verify actions.
- "Cancel" in game chat must not do anything: it only counts with the name, in the follow-up window, while Nova is busy, or while the grid, dictation or a lesson is active.

## Status (end of this session)
- Phases 1–4 done and confirmed on the PC: voice (~0.9–1.3 s), barge-in and echo cancellation on speakers, confirmations, kill switch, files, timers, PC control, routines, HUD, grid on two screens, video control (incl. full screen after the focus fix), keyboard, click by name, numbers, dictation, memory + brain view, tray + autostart.
- Phase 8 (the owner's picks) built: watchers, voice designer, live subtitles, teach by showing. The owner has used teach by showing (after the schema fix); watchers, the voice designer and subtitles aren't confirmed on the PC yet.
- Polish pass done (owner: "he doesn't cancel and he freezes"): universal cancel, tool time limits, "One moment", tick, `media` pause of whatever is playing, "no, I meant X", robust window focus, fullscreen via the player's button with verification. The owner said "it works".
- Spotify playback still parked (PLAN.md has the next steps).
- HUD overhaul (command-centre layout) and phone access (Phase 6) built; neither seen on the PC yet.

## Next (the owner's menu; they pick)
1. ~~Voice lock~~ built (needs the owner's real-voice test and maybe threshold tuning).
2. "Clip that" (NVIDIA instant replay) + game mode (detect a fullscreen game: shorter replies, hold announcements).
3. Morning briefing + health alerts (GPU hot, disk low, battery).
4. ~~Phone access~~ built (needs the owner's test with Tailscale on PC + phone).
5. Gmail + Google Calendar (sending/changing asks first).
6. Others offered: Discord by voice, Steam sale watcher, play-time nudges, clipboard memory, read-to-me, save/restore window layouts, click by description via vision (for games), Spotify playback fix.

## Working conventions
- Keep PLAN.md checklists updated. Commit messages explain the "why". End commit messages with the attribution lines given by the system.
- Write tests with fakes for everything, and reproduce owner-reported bugs as tests (test names/docstrings cite the "owner's case").
- Be honest about what was and wasn't tested on real hardware. Say it in every hand-back to the owner.
- Each hand-back to the owner: what changed in plain words, the update command, what to try, and what to send back if it fails.
