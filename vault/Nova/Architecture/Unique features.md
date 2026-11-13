---
tags: [nova, architecture, features]
---
# Unique features (Phase 8, Oliver's picks)

## "Tell me when..." watchers: `core/watchers.py`
Browser and Steam downloads finishing, an app closing or going quiet, GPU above/below a temperature, CPU calming down, battery full/low, internet back, a web page changing ("this page"). Announced once with a chime; max 10; they expire after a day; listed in the HUD's Timers tab. Needs "tell me / let me know when...".

## Teach by showing: `tools/teach.py`
"Watch what I do" → do it → "Nova, done" → give it a name → later "Nova, <name>". Records clicks (the named button under the mouse via UI Automation), typing, shortcuts and waits by **polling** `GetAsyncKeyState` (no keyboard hooks). **Password boxes are never recorded.** Replay finds the window and the button by name. Saved in `data/learned.json`. Oliver has used it.

## Live subtitles + translation: `voice/subtitles.py`
"Subtitles on" / "translate this" / CC button. Captures the PC's **sound output** (WASAPI loopback, never the mic) → VAD → Whisper with language detection → the local model translates to English → a see-through caption box at the bottom of the screen. Captions are never logged.

## Voice designer: `voice/voicedesign.py`
Blend up to 3 Kokoro voices, speed, pitch (±4 semitones), British/American. Presets: Butler, Deep butler, Young gent, Lady Nova, Nova (American), Movie trailer. HUD Voice tab or "change your voice to deep butler". Saved in `data/voice.json`.
Current mix: bm_george 0.46, bm_lewis 0.31, af_alloy 0.23.

## Voice mouse (grid)
"Show the grid" → "click 14" / "zoom 14" / "double click 3" / "scroll down"; works on either screen; numbers work without "Nova" while it's showing. "Show numbers" puts a number on everything clickable.
