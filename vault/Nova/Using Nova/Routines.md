---
tags: [nova, howto]
---
# Routines

One phrase runs several steps. Routine steps follow the same safety rules as asking for each step separately.

| Routine | Steps |
|---|---|
| **Gaming mode** | open Steam + Discord, volume 60 |
| **Movie time** | pause music, volume 80, Netflix |
| **I'm heading out** | pause music, lock |
| **Goodnight** | pause music, volume 20, sleep (asks first) |

- Add your own in `config/local.yaml` (example in `config/local.example.yaml`). Check them with `.venv\Scripts\python -m assistant routines`.
- Taught routines (teach by showing) and taught shortcuts ("when I say X, do Y") show in the HUD's Routines tab too. See [[Unique features]] and [[Self-learning]].
