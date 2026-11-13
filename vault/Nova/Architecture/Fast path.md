---
tags: [nova, architecture, brain]
---
# Fast path

`assistant/brain/intents.py`: regex matching that runs a tool **without the model**. It's instant and never wrong about which tool to use.

- `match_intent(text)` order: memory → watch → teach → voice/subtitles → timer → grid/numbers → video → keyboard → music rules → "play X".
- `everyday_intent`: dozens of everyday phrases (volume, brightness, conversions, snap/arrange windows, "screen 2", time zones, days until...).
- Also: `tab_intent` (browser tabs), `app_intent` (any app), `browser_intent` (Nova's own browser), `quick_intent` (calculate, weather), `jobs.STATUS/STOP/FOLLOW_UP/ADD_ON`.
- `LocalBrain._small_talk` answers thanks and greetings instantly. A bare "Nova" gets "Yes, sir?".
- `LocalBrain._compound` splits "pause the video and open Spotify" when every part is known.

> [!tip] When adding a fast path
> Extend `tests/test_everyday.py`, the routing table of everyday phrases, and put the tool in `MODEL_HIDDEN`.

Related: [[Local brain]] · [[Voice commands cheat sheet]]
