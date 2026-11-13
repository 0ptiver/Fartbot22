---
tags: [nova, architecture, learning]
---
# Self-learning (lessons)

`assistant/brain/lessons.py`. `LocalBrain.run_turn` is a learning layer around `_turn`.

- **Corrections** ("no, I meant X", "that's wrong, open kbb.com") become lessons: the exact tool calls that worked for that request.
- **"When I say X, do Y"** teaches a shortcut. Shortcut-shaped memories are moved into shortcuts (`_move_shortcuts`, `lessons._TEACH`).
- **Preferences** go to [[Memory]].
- **Replay:** next time the same request is replayed instantly, with no model and no Claude.
- Successful [[Work it out]] runs are learned too (not `screen_click` runs, not [[Background jobs]]).
- Stored in `data/lessons.json`. Tool `learn` (list/forget). Shown on the HUD's Brain page.
- **Teach by showing** is separate: see [[Unique features]].
