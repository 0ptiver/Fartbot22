---
tags: [nova, overview]
---
# What is Nova

Nova is a personal assistant you **talk to**, like JARVIS. Say "Nova, ..." and it does it.

- **Personality:** calm, concise British butler. Calls Oliver "sir". Knows Oliver made it and is in charge (`assistant.owner_name: Oliver`).
- **Hears you** with a local Whisper model on the GPU. Only answers when you say its name (see [[Voice pipeline]]), and can be locked to your voice only ([[Voice lock]]).
- **Speaks** with Kokoro, a local voice model, in about 70 ms per sentence on the GPU.
- **Thinks** with a small local model (free). Hard things go to Claude on your Pro plan. See [[How Nova thinks]].
- **Does things on the PC:** apps, windows, browser tabs, clicking buttons by name, typing, volume, Spotify, video, timers, files, power. See [[Tools and safety]].
- **Sees the screen** itself with Windows' built-in text reader ([[Screen reading]]), and notices error boxes and frozen apps ([[Screen watch and windows]]).
- **Works on whole jobs** in the background ("complete this assignment for me") while still chatting ([[Background jobs]]).
- **Learns:** correct it once and it remembers ([[Self-learning]]). Remembers facts you tell it ([[Memory]]).
- **Has a window** (the HUD) with a glowing orb, a live feed of what it's doing, timers and memories ([[HUD window]]).
- **Works from your phone** over Tailscale, with voice ([[Phone access]]).
- **Lives in the tray** and starts with Windows ([[Tray and autostart]]).

## Design rules it follows
1. **Never claim it did something it didn't.** Every action is checked (window really moved, song really playing, key press really changed something). See [[Lessons learned]].
2. **Privacy first.** Nothing is exposed to the internet, keys live in Windows Credential Manager, and no audio is saved.
3. **Free or subscription only.** No API fees.
4. **Short and to the point.** Oliver hates chatter.

Related: [[Owner and PC]] · [[Request flow]]
