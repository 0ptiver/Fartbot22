# CLAUDE.md — handoff notes for Claude

Read this first. Then read PLAN.md (phase checklist + history) and README.md (user-facing docs).

## What this is
**Nova**: a voice-first personal assistant (JARVIS-style) for the owner's Windows 11 gaming laptop.
British-butler personality, calls the owner "sir". The code package is `assistant/`, so renaming is config-only.
Branch: `claude/jarvis-voice-assistant-lhnfza` (commit and push there after every milestone).

## The owner
- Not a developer. Explain things simply, give copy-paste PowerShell commands, and ask for screenshots.
- They test on the real PC and send screenshots. **I can't run anything on their hardware.**
- Hardware: i9-14900HX, RTX 5070 Laptop 8 GB (Blackwell), 32 GB RAM. Mic: Fifine SC3 (`input_device: 3` in their `config/local.yaml`). Output: **Realtek speakers**, so echo matters. Computer name `LAPTOP-VKHGAOUV`.
- Has Claude Pro, Ollama, Spotify Premium, and Claude Code installed and signed in. Budget: free or subscription only, no API fees.
- Cares a lot about **security/privacy**. Wants an **interactive interface (HUD)** at the end.
- Tell them to update with `powershell -ExecutionPolicy Bypass -File scripts\update.ps1`. **Never tell them to run `pip install` directly**: it reinstalls CPU onnxruntime over onnxruntime-gpu and the voice drops to ~650 ms (a warning is printed at startup when that happens).

## My sandbox (Linux cloud container)
- Blocked: huggingface.co, github release downloads, ollama.com, developer.spotify.com. PyPI works. There's no GPU, audio device or Ollama.
- So real models can't run here. Test with fakes: `tests/fakes.py` (Claude), a fake Ollama (httpx MockTransport), a fake `claude` executable, a fake Spotify API, and `tests/data/what_time.wav` (espeak) with real Silero VAD.
- `.venv` (Python 3.12, `uv`). Run `.venv/bin/python -m pytest -q` (~200 tests, ~6 s) and `.venv/bin/python -m pyflakes assistant tests`.
- **Beware unbounded loops in tests**: fixtures set delays to 0, and a while loop keyed on elapsed time hangs pytest. Use `timeout 100` when running tests.
- `pkill -f pytest` also kills your own shell (exit 144).

## Architecture (assistant/)
- `core/` config (`config/config.yaml` defaults + git-ignored `config/local.yaml` overrides, deep-merged), secrets (env/.env then Windows Credential Manager via keyring), FastAPI server (`/ws`, local token + Origin/Host checks, 127.0.0.1 only), session, conversation.
- `brain/`
  - `local.py` `LocalBrain` = everyday model in Ollama (`qwen3:4b-instruct-2507-q4_K_M`; the **instruct** variant, because the thinking variants read their reasoning aloud). Includes a ThinkFilter, a warm-up with identical settings (for the prompt cache), a nudge when the model promises without acting, a direct "ask Claude…" route, and the `intents.py` fast path.
  - `intents.py` = regex fast path for music commands, with no model call.
  - `expert.py` = hard tasks via the **official Claude Code CLI on the owner's subscription** (`claude -p`, task on stdin, API keys stripped from env, `--permission-mode dontAsk`, tools WebSearch/WebFetch/Read with Read confined to an empty workspace, `--strict-mcp-config`). Never use `--bare`: it ignores subscription login.
  - `llm.py` `Brain` = Claude API backend (optional, paid).
  - Vision ("what's on my screen") goes to Claude Code by default. A local VL model evicts the chat model from 8 GB VRAM.
- `tools/video.py` browser video control: GSMTC media sessions via winrt (exact play/pause, title) + focus the browser window by title and press f/Esc/m/j/l. Fast path `intents.video_intent` splits "X and Y".
- `background.py` supervisor (`assistant background`), autostart (HKCU Run), log_to_file (stdout -> data/logs/nova.log), single-instance mutex; `tray.py` pystray icon + TrayActions; `voice --background` wires them in cli.run (returns exit code 0 quit / 3 restart / 1 failed start).
- `tools/teach.py` teach by showing: InputPoller (GetAsyncKeyState polling, no hooks) -> build_steps -> Teacher (start/stop/save/cancel, data/learned.json) -> replay_click/replay_keys; routines.active merges learned; LocalBrain checks teach_intent with the Teacher's state first; ToolRegistry.observers.
- `voice/subtitles.py` live subtitles (soundcard loopback, own SileroVAD, Segmenter, WhisperSTT.transcribe_any, Translator via Ollama with identical options); caption box = TkOverlay.captions (same Tk thread as the grid); created in voice/cli.make_subtitles, tool in tools/voice.py.
- `voice/voicedesign.py` VoiceDesign (mix/speed/pitch/lang, data/voice.json), blend, repitch; KokoroTTS.set_design/preview; VoiceLoop.set_voice/preview_voice; HUD Voice tab = `hud/static/voice.js`; `tools/voice.py` set_voice presets. `core/watchers.py` + `tools/watch.py` "tell me when".
- `core/memory.py` MemoryStore (SQLite+FTS5, secret refusal, same-subject replace, listeners) + `tools/memory.py`; LocalBrain._memories adds relevant facts to turn_context. HUD brain view = `hud/static/brain.js` (canvas), fed by "memories"/"memory_used" events (server.watch_memory).
- `tools/keyboard.py` type_text/press_keys (SendInput; terminal/Run box asks first) + dictation tool (VoiceLoop.set_dictation/_dictate). `tools/uia.py` click_element/show_numbers via UI Automation (comtypes), labels drawn by the grid overlay (GridController.labels). Spoken shortcuts in `intents.keyboard_intent`.
- `tools/grid.py` voice mouse grid (Tk overlay thread + ctypes mouse; fast-path phrases in intents.grid_intent; no name needed while visible via VoiceLoop._grid_command). Preview the overlay under Xvfb with /usr/bin/python3.12 (has tkinter; the project venv doesn't).
- `tools/pc.py` system_status/lock_pc/power(confirm)/cancel_shutdown/window/open_website (Win32 in swappable backends). `tools/routines.py` config-defined routines, registered last in build_registry.
- `tools/` registry (JSON schema, risk safe/confirm/blocked, audit log `data/audit.jsonl`, remote policy), system (time/volume/open_app), screen, expert(escalate), web (DuckDuckGo via ddgs), music (Spotify + media keys).
- `voice/` VoiceLoop (`pipeline.py`):
  - Silero VAD endpointer with speculative STT on pause.
  - faster-whisper large-v3-turbo on cuda.
  - Kokoro TTS on GPU via onnxruntime-gpu, using `models/kokoro-v1.0.gpu.onnx`: STFT rewritten as a Conv by `tts/onnx_fix.py`, with a +1e-20 bias for PyTorch-style edge-bin phase. Accepted by *sound* (spectral distance), because a sample-exact match is impossible: atan(imag/real) sign flips.
  - Kokoro takes ~72 ms on GPU, ~240 ms with the original model, ~650 ms on CPU.
  - Wake mode: say "Nova" in the sentence (transcript-based, no hotkey; the `keyboard` hook glitched the owner's keyboard). 6 s follow-up window that doesn't chain.
  - Barge-in "verified" (echo-aware), stop words, pause-and-continue merge.
  - AEC: WebRTC AEC3 via `livekit` (`voice/aec.py`), plus a text echo check (`textnorm.py`) used only within 2 s after speaking and never for requests that use the name.
  - `speechtext.py` speaks symbols as words. Latency report after each reply.
- `integrations/spotify.py`: PKCE login (`assistant spotify login`), tokens in keyring, prefers this PC's device, verifies playback.
- CLI: `python -m assistant {voice|chat|serve|doctor [--full]|models [--recheck|--debug-stft]|ttsbench [--profile]|mictest|devices|spotify ...|tools|audit|secrets set NAME}`.

## Status (end of last session)
- Phases 1–3 done and verified by the owner: voice works, latency ~0.9–1.3 s, barge-in and echo cancellation work on speakers.
- Phase 4 started. **Spotify playback is parked** (the owner's call): play is "accepted" but nothing plays. Pause/next/now-playing work. Next steps are in PLAN.md under "Spotify playback: parked".
- Phase 4 done in code: safety core (voice confirmations, "stand down" kill switch, folder allowlist), file tools, timers/reminders, PC control, routines. The owner said "it seems to be working" after the PC-control update; routines are not yet tried on the PC.
- HUD built (assistant/hud/, web page in an Edge --app window, served inside `assistant voice`); not yet tried on the PC. Screenshot it locally with playwright + executable_path=/opt/pw-browsers/chromium (a scratch venv; the project venv has no playwright).
- **Next:** HUD extras (settings editor, audit view, tray) or memory (short-term summaries, long-term SQLite, "forget that", editable in the HUD). Later: Gmail/Calendar, phone access (Tailscale + auth + 2FA), morning briefing, autostart, Spotify playback.

## Working conventions
- Keep PLAN.md checklists updated. Commit messages explain the "why". End commit messages with the attribution lines given by the system.
- Write tests with fakes for everything, and reproduce owner-reported bugs as tests (see test names citing the "owner's case").
- Be honest about what was and wasn't tested on real hardware.
