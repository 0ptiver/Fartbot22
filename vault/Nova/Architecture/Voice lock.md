---
tags: [nova, architecture, voice, security]
---
# Voice lock

`assistant/voice/voiceprint.py`, `VoiceLock`. Nova only obeys Oliver's voice.

- "Nova, learn my voice": read 5 short lines. Stores a voiceprint (256 numbers, **no audio**) in `data/voiceprint.json`.
- Encoder: numpy GE2E from the Resemblyzer PyPI wheel (torch-free).
- Checks run in parallel with speech-to-text; enforced in `_handle_utterance` and `_barge_verify`.
- **Never ignore Oliver silently** ("straight up ignoring me"): near misses are spoken and shown with a "That was me" button. Stand down, stop and cancel always pass. "Stop" while Nova is busy skips the lock.
- Speech heard over Nova's own voice matches worse, so the lock is looser there (otherwise you can't interrupt).
- A **new mic changes the voiceprint**: "learn my voice again" adds one per mic (up to 5).
- "Voice lock off/on", "forget my voice", "is voice lock on?" Strictness slider on the HUD's Voice tab (log showed bar 0.66).
