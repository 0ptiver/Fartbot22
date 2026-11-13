---
tags: [nova, architecture, hud]
---
# HUD window

`assistant/hud/`: Nova's window, served from inside `assistant voice` on `127.0.0.1:8766` and shown in an Edge `--app` window with its own profile (`data/hud-browser`). Opens at 1180×760.

- **Left rail:** Chat, Activity, Timers, Routines, Brain, Voice, Phone. Under 860 px it moves to the bottom.
- **Home** (`home.js`): live action feed (tool + arguments), captions, Now playing with controls, Up next, gauges, memories; cards pulse when touched.
- **Orb** (`orb.js`, a particle sphere; `NovaOrb.add(canvas)`): its colour follows Nova's state (`--glow`). Also used on the phone.
- **Right panel:** Stop (Esc), Stand down, Mic, Yes/No confirm card (Y/N keys), Up next, CPU/RAM/GPU vitals.
- Nova can switch pages itself: "show me my timers" (`tools/hudnav.py`).
- Server pushes status 10×/s, timers once a second, vitals every 3 s.
- **Security:** 127.0.0.1 only, a fresh key per run in the URL fragment, Host/Origin checks, strict CSP, text only (no HTML injection).
- Reopen: `.venv\Scripts\python -m assistant hud`. Off: `voice --no-hud` or `hud: {enabled: false}`.
