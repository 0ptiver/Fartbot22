---
tags: [nova, architecture, voice]
---
# Voice pipeline

`assistant/voice/pipeline.py`, `VoiceLoop`. Mic → words → Nova → voice.

| Stage | What | Notes |
|---|---|---|
| Mic | `MicStream` (sounddevice, 16 kHz) | the saved device is checked at start (`audio.pick_device`); a renumbered one falls back to Windows' default |
| Echo cancellation | WebRTC AEC3 (`aec.py`, livekit) | removes Nova's own voice from the mic on speakers |
| End of speech | Silero VAD, ~400 ms silence | speculative STT starts during your pause |
| Speech-to-text | faster-whisper `large-v3-turbo` on CUDA (int8_float16) | falls back to the CPU |
| Wake | by name in the transcript (`wake.py`), no hotkey | name near the start or end |
| Voice | Kokoro on the GPU (`models/kokoro-v1.0.gpu.onnx`; STFT rewritten as a Conv by `tts/onnx_fix.py`) | ~72 ms per sentence |

Typical end-to-end: **0.9 to 1.3 s** from when you stop talking to Nova's first word.

## Behaviours
- **6 s follow-up window** after a reply: one more request without the name. It doesn't chain. Speech without the name must be clearly for Nova (a command, a follow-on, or an answer to Nova's question; `_for_me`, `wake.follows_on`). Over Nova's own voice, only the name, "stop" or a command count.
- **Barge-in:** talk over Nova to interrupt (verified so its own echo doesn't count). "Stop", "cancel", "never mind" just stop it.
- **Pause and carry on:** "Nova, what's the weather... in Chicago" is merged when it's clearly unfinished (`wake.unfinished`/`continues`).
- **Universal cancel** (`cancel_last`): stop → hide grid → drop lesson → end dictation → stop a job → undo the last action within 2 min (timers, window arranging...). "Cancel" in game chat does nothing unless said with the name, in the follow-up window, while Nova is busy, or while the grid, dictation or a lesson is active.
- **Stand down / wake up** (kill switch; needs the name, because "stand down!" is GTA RP talk).
- **Spoken confirmations** ("yes"/"no") for risky actions.
- **Announcements** (timers, watchers, job results, screen alerts) play with a chime and **can be interrupted** (`_announcing`).
- A soft **tick** means it heard you. "One moment, sir." after 1.8 s of silence.
- **Dictation**: everything said is typed ("new line", "scratch that", "stop dictation").

Other files: `subtitles.py`, `voicedesign.py`, `speechtext.py`, `textnorm.py`, `wake.py`, `commands.py` (yes/no, stand down, cancel).

Related: [[Voice lock]] · [[Unique features]]
