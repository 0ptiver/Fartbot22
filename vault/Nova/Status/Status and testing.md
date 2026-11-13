---
tags: [nova, status]
updated: 2026-11-13
---
# Status and testing

> [!info] Legend
> "Confirmed on the PC" = Oliver tried it and it worked. Everything else passes the tests here but hasn't been seen on the real hardware yet.

- Phases 1–4 done and confirmed on the PC: voice (~0.9–1.3 s), barge-in and echo cancellation on speakers, confirmations, kill switch, files, timers, PC control, routines, HUD, grid on two screens, video control (incl. full screen after the focus fix), keyboard, click by name, numbers, dictation, memory + brain view, tray + autostart.
- Phase 8 (the owner's picks) built: watchers, voice designer, live subtitles, teach by showing. The owner has used teach by showing (after the schema fix); watchers, the voice designer and subtitles aren't confirmed on the PC yet.
- Polish pass done (owner: "he doesn't cancel and he freezes"): universal cancel, tool time limits, "One moment", tick, `media` pause of whatever is playing, "no, I meant X", robust window focus, fullscreen via the player's button with verification. The owner said "it works".
- Spotify playback reworked (polish pass 3); needs the owner's test.
- HUD overhaul (command-centre layout) and phone access (Phase 6) built; neither seen on the PC yet.
- Latest (this session, owner away testing in bits): the owner reported bugs from the PC and each was fixed with tests: `<context>` leaking into replies; reminders unknown to the model; memories recited (now only relevant ones; shortcut-memories become shortcuts); every sentence checked for made-up actions; refusals ("I can't help with that") answered by Claude instead; volume COM "Cannot change thread mode" (`core/com.py`); jumbled typing into Notepad (paste + real keys + `write_document`); whole jobs moved to the background with live status. New abilities: Nova reads the screen himself (Windows OCR) and clicks visible text; "complete this assignment / do my homework" whole jobs. A security pass hardened phone access and the Claude hand-off. The owner confirmed a whole background job worked on the PC (assignment answered, saved in Documents\\Nova, "All done" announced). Then: follow-ups to a job go back to Claude, "already done" claims and made-up tags caught, announcements interruptible, honest Ctrl+V: not yet confirmed on the PC.
- Earlier: talk to Nova from the phone like at the desk (voice in, Nova's voice out, PC control with Yes/No on the phone, HTTPS via Tailscale certs): the owner confirmed it works. Then Nova's bubble (orb.js) on the phone, tap to talk: not yet seen on the phone.
- Earlier: automatic "work it out" hand-off to Claude with Nova's tools + learning (checked end to end here with the real CLI on a harmless tool; not yet on the PC). Video play/pause via the player's own button when Windows' media list is stuck.
- Earlier: owner's browser + any-app control (`my_browser`, `app`), every step checked. Not yet tried on the PC.
- Polish pass 4 (owner away): context-window fitting (`brain/fit.py`, the likely "bugs out after 5 minutes" cause), 22 more direct phrases, checked open/close/quit, summarise page, conversions, brightness. Not yet tried on the PC.
- Earlier: "more brains" (situation context: window in front, Nova's browser, media → `brain/situation.py`; `press_keys` names the target window and verifies tab/nav shortcuts by title change; Nova's-browser shortcuts via `browser.shortcut`) and owner identity (`assistant.owner_name: Oliver`, prompt says Oliver made Nova and is in charge; risky-action confirmations stay). Not yet tried on the PC.

## Latest fixes (after the notes above)
- Tray Nova started from PowerShell now says it's running (the window looked frozen).
- A renumbered mic (`input_device: 3` → speakers) no longer stops Nova starting: falls back to the default mic; the doctor checks the configured mic.
- Long jobs: "Claude Code failed: exit 1" was Claude running out of steps → now carries on in fresh runs (up to 4).
- Only Claude Code's own sign-in errors count as "not signed in"; "OAuth session expired" recognised with the fix-it steps.
- Whole-job requests with extra instructions (", don't press finish") run as background jobs.
- None of these is confirmed on the PC yet.
