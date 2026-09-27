# Nova

A real-time, voice-first personal assistant for Windows. Wake phrase **"Hey Nova"** (coming in Phase 3).
It's calm and concise and calls you "sir", in a British butler style. It runs on your PC and controls it through a tool layer with safety rules.

See [PLAN.md](PLAN.md) for the architecture, chosen stack, costs and phase checklist.

**Current status: Phase 2 (voice loop).** Hold a hotkey and talk. The everyday model, speech-to-text and text-to-speech all run on your PC for free. Hard tasks go to your Claude subscription.

## How it thinks: free by default

| Job | Who does it | Cost |
|---|---|---|
| Everyday chat, PC control, quick facts, web lookups | **A local model in Ollama** (`qwen3:4b-instruct-2507`) on your GPU | free |
| "What's on my screen?" | **Your Claude subscription** (on 8 GB GPUs a local vision model pushes the chat model out) | included in Pro |
| Hard tasks: research, analysis, writing, code, "ask Claude…" | **Your Claude Pro subscription**, through the official Claude Code CLI | included in Pro |
| Optional: the Claude API instead of either one | `brain.backend: anthropic` / `expert.backend: anthropic` | paid per token |

How the hand-off works: Nova runs the real, unmodified `claude` program, signed in with your own account. Nova never sees or stores your Claude login.
Hard tasks count against your Pro plan's usage limits, so keep them for things that need it.
Claude Code is locked down for this: it gets web search and web fetch only, no shell, no file edits, no MCP servers, and it runs in an empty folder of its own.

## Updating

Always update with the script. **Don't run `pip install` yourself**: pip puts the CPU version of the voice engine back over the GPU one.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\update.ps1
```

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

## Voice

```powershell
.venv\Scripts\python -m assistant voice
```

**Just talk and say "Nova".** For example: "Nova, what time is it?", "Hey Nova, open Spotify", or "What's the weather tomorrow, Nova?"
- It ignores speech that isn't addressed to it. The name has to be near the start or the end of the sentence.
- After it answers a "Nova, …" request, you have **6 seconds for one more request without the name**, e.g. "thanks" or "and tomorrow?". That doesn't chain, so a conversation with someone else in the room isn't answered.
- **"Ask Claude …"** always goes straight to your Claude subscription.
- Say just "Nova" and it answers "Yes, sir?", then waits for your request.
- There are no hotkeys or keyboard hooks. Everything runs locally, and no audio is saved.
- **Talk over it to interrupt.** Nova stops and answers your new request. Say **"stop"**, "cancel" or "never mind" to just stop it.
- **Echo cancellation** (the same WebRTC echo canceller video calls use) removes Nova's own voice from the mic, so it doesn't hear itself on speakers. As a backup, it only stops for words that don't match what it just said. With headphones, `barge_in.mode: fast` stops it the instant you speak.
- **Pause and carry on.** "Nova, what's the weather… in Chicago tomorrow" is treated as one request, as long as you carry on within 2.5 s and before it starts answering.
- **Faster replies.** Whisper starts transcribing during your pause, so the transcript is usually ready the moment it decides you've finished.

Other modes (`--mode` or `voice.mode` in `config/local.yaml`): `open_mic` answers everything it hears, and `ptt` means hold `Ctrl+Alt+Space` while talking.

- **Latency**: after every reply it prints how long each stage took, and the total from when you stopped talking to when it started speaking.
- **Test your mic**: `python -m assistant mictest` shows a live level meter and what Whisper heard.
- **Pick a mic or speakers**: run `python -m assistant devices`, then set `voice.input_device` in `config/local.yaml`.
- **Test without a mic**: `python -m assistant voice --wav my_question.wav --out reply.wav`.

## Music (Spotify)

Without setup, "Nova, pause", "next song" and "go back" work for any app through the media keys.

For full Spotify control ("play some Drake", "play my liked songs", "play the last song I listened to", "what's playing?", "queue Hotline Bling", "shuffle on", "Spotify volume 40"), link it once:

```powershell
.venv\Scripts\python -m assistant spotify login
```

1. Go to https://developer.spotify.com/dashboard, log in, and click **Create app**. Use any name and description, set the **Redirect URI** to `http://127.0.0.1:8888/callback`, and tick **Web API**.
2. Open the app's **Settings**, copy the **Client ID**, and paste it into the prompt.
3. Your browser opens Spotify's own approval page. Click **Agree**.

If it says Spotify accepted but nothing is playing, run `python -m assistant spotify devices` to see which players Spotify knows about and which one Nova picks. It prefers the Spotify app on this PC.

Nova never sees your Spotify password. It uses PKCE, so there's no client secret either, and only a revocable token is stored, in Windows Credential Manager. To unlink: `python -m assistant spotify logout`, and remove the app at spotify.com/account/apps. Playback control needs Spotify Premium.

Speech stack (all switchable in `config/config.yaml` → `voice`):

| Stage | Default | Notes |
|---|---|---|
| Voice detection | Silero VAD | `end_silence_ms: 400` is how long a pause ends your turn |
| Speech-to-text | faster-whisper `large-v3-turbo` on your GPU | falls back to CPU automatically if CUDA fails. `small.en` is faster but less accurate |
| Text-to-speech | Kokoro, voice `bm_george` (British male) | also `bm_lewis`, `bm_daniel`, `bf_emma`. Runs on the GPU when `onnxruntime-gpu` is installed (the installer does this on NVIDIA PCs), otherwise on the CPU |
| Cloud option | Deepgram (`stt.provider: deepgram`) | needs `DEEPGRAM_API_KEY`, ~$0.46/hour |

### Things to try

| Say | Tool |
|---|---|
| "What time is it?" | `get_time` |
| "Turn the volume down a bit" / "set volume to 30" / "mute" | `volume` |
| "Open Spotify" / "open Steam" / "open notepad" | `open_app` (aliases in `config/config.yaml`, then Start Menu) |
| "What's on my screen?" / "read the error on my screen" | `look_at_screen` (screenshot → Claude vision) |
| "Who won the game last night?" / "weather in Chicago tomorrow" | `web_search` (Claude server tool) |
| "How's my PC doing?" / "how hot is my GPU?" / "how much disk space?" | `system_status` |
| "Lock my PC" | `lock_pc` (straight away) |
| "Put the PC to sleep" / "restart" / "shut down" | `power` (asks first; restart/shutdown wait 60 s) |
| "Cancel the shutdown" | `cancel_shutdown` |
| "Switch to Chrome" / "minimise Discord" / "show the desktop" | `window` |
| "Open YouTube" / "search YouTube for lofi" / "Google the weather" | `open_website` (http/https only) |
| "Set a timer for 10 minutes for the pasta" / "remind me at 7 pm to call mum" | timers (announced with a chime) |
| "Find my tax return" / "read my shopping list" | `find_files`, `read_file` (your own folders only) |
| "Nova, stand down" … "Nova, wake up" | kill switch / standby |
| "Click Subscribe" / "click the search box" / "press the sign in button" | clicks it by name in the app you're using |
| "Show numbers" … "click 7" (or just "7") | a number on everything clickable |
| "Type hello there" / "press enter" / "press control c" / "press tab three times" | keyboard |
| "Copy" / "paste" / "undo" / "save" / "new tab" / "close tab" / "go back" / "refresh" / "switch windows" | shortcuts |
| "Minimise this" / "snap this to the left" / "move this window to the other screen" | the window you're using |
| "Start dictation" … (talk) … "new line" / "scratch that" / "stop dictation" | everything you say is typed |
| "Tell me when my download finishes" / "…my game finishes downloading" / "…Blender is done" / "…my GPU cools down" / "…the battery is full" / "…the internet is back" / "…this page changes" | Nova watches and speaks up (see them in the window's Timers tab) |
| "Subtitles on" / "translate this" / "what are they saying?" / "subtitles off", or **CC** in the window | live captions of your PC's sound (game chat, videos), translated into English |
| "Change your voice to deep butler" / "use your normal voice", or the window's **Voice** tab | design Nova's voice: mix voices, speed, pitch, accent, preview, save |
| "Remember that my sister's birthday is June 3" / "what do you remember?" / "forget that" | memory (on this PC only; never passwords) |
| **Brain** button in the window | everything Nova remembers around a glowing brain: search, click to forget, teach it new things |
| "Show the grid" … "click 14" / "zoom 14" / "double click 3" / "scroll down" / "hide the grid" | voice mouse (numbers work without "Nova" while the grid is showing) |
| "Show the grid on screen 2" / "on the other monitor" … "next screen" | voice mouse on another screen (screen 1 = your main one) |
| "Full screen the video and press play" / "pause the video" / "skip ahead" / "exit full screen" | `video`: finds the video playing in your browser by itself |
| "Gaming mode" / "movie time" / "I'm heading out" / "goodnight" | routines (see below) |
| "Work out the monthly payment on a $20k loan at 6% over 5 years" | `escalate` → Claude Opus 5 |

`/reset` clears the conversation. `/quit` exits. `Ctrl+C` interrupts a reply.

### Routines

One phrase runs several steps. Built in: **gaming mode** (Steam + Discord, volume 60), **movie time**
(pause music, volume 80, Netflix), **I'm heading out** (pause music, lock), **goodnight** (pause music,
volume 20, sleep, which asks first). Add your own in `config/local.yaml` (example in
`config/local.example.yaml`), then check them with `.venv\Scripts\python -m assistant routines`.
Routine steps follow the same safety rules as asking for each step separately.

## Always on (tray icon)

```powershell
.venv\Scripts\python -m assistant autostart on      # start with Windows (off: autostart off)
.venv\Scripts\pythonw -m assistant background       # start it now, in the tray
```

Nova then lives by the clock: its icon shows what it's doing (blue listening, purple thinking,
grey standing down, red mic off). Right-click it for Open / Microphone off / Stand down /
Start with Windows / Open log / Restart / Quit. If it crashes, or Ollama isn't ready yet after
boot, it starts itself again. Its output goes to `data\logs\nova.log` instead of a console.
Only one Nova runs at a time. `scripts\update.ps1` stops it for the update and starts it again.

## The window (HUD)

`python -m assistant voice` also opens Nova's window: the orb shows what Nova is doing
(listening, thinking, speaking, waiting for a yes/no, standing down), and below it are the
conversation, what Nova did (Activity), your timers (with cancel buttons) and your routines
(one click each). You can type to Nova at the bottom. **Yes/No** buttons appear whenever Nova asks
before doing something (or press Y / N). **Stop** (or Esc) cuts Nova off, **Stand down** is the
kill switch, and the microphone button mutes Nova's hearing.

Closed it? `.venv\Scripts\python -m assistant hud` reopens it. Don't want it?
`voice --no-hud`, or `hud: {enabled: false}` in `config/local.yaml`.

It only works on this PC: it listens on 127.0.0.1, needs a new secret key each time Nova starts,
refuses other websites, and runs in its own browser profile with no extensions.

## Configuration

Defaults live in `config/config.yaml`. **Put your own changes in `config/local.yaml`** (copy `config/local.example.yaml`). It overrides the defaults, and `git pull` never touches it. You only need to list what you change.

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
| `qwen3-vl:4b` (vision, optional) | ~3.5 GB | only with `local.vision: ollama`. On 8 GB it evicts the chat model (~4 s reload) |

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
| Slow "tts first audio" in the latency report | Run `powershell -ExecutionPolicy Bypass -File scripts\enable_gpu_tts.ps1` to put the voice on the GPU, then `python -m assistant ttsbench`. On CPU-only PCs, use the thread count or int8 model it recommends. |
| It says "I'll check that" and then does nothing | Nova now nudges the model to actually do it. If it keeps happening for a request, tell me which one, or start with "Ask Claude…". |

## Development

```bash
python -m pytest -q
```

The tests use a scripted fake Claude client (`tests/fakes.py`), so they need no API key and cost nothing.
The voice integration test runs a recorded clip (`tests/data/what_time.wav`) through the real Silero VAD and the whole pipeline, with fake STT and TTS.
