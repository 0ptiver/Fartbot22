---
tags: [nova, architecture, memory]
---
# Memory

`assistant/core/memory.py`: SQLite + FTS5 in `data/memory.db`, on this PC only.

- "Remember that my sister's birthday is June 3" / "what do you remember?" / "forget that".
- **Refuses secrets** (passwords, card numbers). A new fact about the same subject replaces the old one. Listeners update the HUD.
- **Only relevant memories go to the model** (`MemoryStore.for_turn`: a full-text search with filler words removed). Sending all of them every turn made the 4B model recite them.
- The HUD's **Brain** page shows memories around a glowing brain: search, click to forget, teach new things.

Related: [[Self-learning]] · [[HUD window]]
