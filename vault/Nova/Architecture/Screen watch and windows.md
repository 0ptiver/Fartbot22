---
tags: [nova, architecture, screen]
---
# Screen watch and window arranging

"Seamless PC control", Oliver and Nova's pick.

## Screen watch: `core/screenwatch.py`
- Every 3 s: new **error dialogs** (`WINDOWS.dialogs()`: class #32770, owned pop-ups; text read by OCR; only when it has problem words) and **frozen apps** (`IsHungAppWindow`, 3 checks in a row).
- Says it **once** ("Steam says: Disk write error. Say 'fix it' and I'll sort it out."); the same message isn't repeated within 2 min; quiet while a full-screen game is in front.
- Replies, while the problem is still on screen (within 3 min):
  - "fix it" → Claude with the error text
  - "fix this error" with nothing spotted → Claude looks
  - "what does it say?" → reads it
  - "ignore it" → "Very good, sir."
  - "close it" → for a frozen app, quits that app
- Config `screen_watch` (enabled, interval_s). Windows only.

## Window arranging: `pc.arrange`
| Say | Action |
|---|---|
| "Snap Firefox to the left" / "put Discord on the right" | `snap_left` / `snap_right`: half of the screen it's on, flush (DWM invisible borders compensated) |
| "Move Spotify to my other screen" / "to screen 2" / "main screen" | `other_screen` (stays maximised if it was) |
| "Put Spotify and Discord side by side" | `side_by_side` |
| "Cancel" / "undo that" | `undo_arrange` puts them back |

Every move is checked (`_landed`): a window that won't move is reported, never claimed. Tests: `tests/test_screenwatch.py` (FakeDesk).
