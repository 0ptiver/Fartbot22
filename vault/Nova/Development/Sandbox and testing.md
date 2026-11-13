---
tags: [nova, dev]
---
# Sandbox and testing

Claude develops Nova in a **Linux cloud container**, not on the PC. It can't run anything on Oliver's hardware, so every Windows part sits behind a swappable backend and is tested with fakes. Oliver tests on the real PC.

- Blocked: huggingface.co, github release downloads, ollama.com, developer.spotify.com. PyPI works. There's no GPU, audio device, Windows or Ollama.
- So real models and Win32 can't run here. Every Windows part sits behind a swappable backend (pc.WINDOWS, pc.POWER, grid MouseBackend/TkOverlay, keyboard.KEYBOARD, uia.UIA, video.MEDIA, teach InputPoller) and is tested with fakes.
- `.venv` (Python 3.12, `uv`). Run `timeout 100 .venv/bin/python -m pytest -q` (~800 tests, ~50 s) and `.venv/bin/python -m pyflakes assistant tests`.
- **Beware unbounded loops in tests**: fixtures set delays to 0, and a while loop keyed on elapsed time hangs pytest. Always use `timeout 100`.
- `pkill -f <pattern>` kills your own shell if the pattern matches the command line (exit 144). Let background demo servers time out instead.
- conftest autouse fixtures redirect `voicedesign.VOICE_FILE` and `teach.LEARNED_FILE` to tmp; `settings.memory.path` is tmp too. Keep file paths looked up at call time (not as default args) so tests can redirect them.
- Visual checks: HUD screenshots with Playwright from a scratch venv (`pw/`) using `executable_path="/opt/pw-browsers/chromium"`; Tk overlays under `Xvfb :99` with `/usr/bin/python3.12` (has tkinter) in a scratch venv with `--system-site-packages` (`tkv/`). Both live in the scratchpad, not the repo.
- Checking Windows-only wheels: `pip download --only-binary=:all: --platform win_amd64 --python-version 3.12 <pkg>` and inspect the .whl.

## Tests
- ~1,040 tests (`tests/`), about 70 s: `timeout 200 .venv/bin/python -m pytest -q`; lint: `.venv/bin/python -m pyflakes assistant tests`.
- Owner-reported bugs are reproduced as tests whose names/docstrings cite the "owner's case".
- Key test files: `test_everyday.py` (phrase routing table), `test_agent.py` (fake agent + fake claude script; jobs), `test_local_brain.py`, `test_screenwatch.py` (FakeDesk), `test_mybrowser.py` (pretend Firefox), `test_ocr.py`, `test_hardening.py` (security), `test_devices.py`.
- **Tests must never run the real Claude CLI** (conftest turns the agent off).
