---
tags: [nova, status, history]
---
# History

How Nova was built, in the order it appears in PLAN.md (the full checklists are in `PLAN.md` in the repo). Quotes are Oliver's requests.

- Phase 1 — Text MVP
- Phase 2 — Voice loop
- Phase 2.5 — Free brain (owner request)
- Phase 2.6 — Hands-free by name (owner request: remove the hotkey)
- Phase 3 — Real-time feel
- Phase 3 — verified by the owner
- Phase 4 — Full tool layer + safety
- Phase 5 — Desktop HUD + memory
- Phase 6 — Remote access (owner: "let's add remote access from my phone")
- Phase 7 — Proactive + polish
- Polish pass (owner: "he doesn't cancel things, and he pauses and freezes")
- Polish pass 5 (owner: "go ahead and go through and polish up everything you can")
- Made-up claims (owner: "he gets more and more stupid the more you do, can you just actually fix the problems")
- Work it out + learn (owner: "it keeps saying I can't do this and that ... allow it to self learn and be able to work through things"; chose automatic)
- Scam lookalike (owner: "make it fact check what the real url is before it goes to it, it just opened this scam website like 50 times")
- Wake word + browser slips (owner: "he answers everything I say when I don't say his name"; "I asked it to close a tab and it closed the browser ... now the hud is not moving")
- Your browser and any app (owner: "he still struggles doing simple tasks like open a new tab on my browser, I want him to control and navigate my browser and any app effortlessly")
- Polish pass 4 (owner: "continue polishing, I am not home to test")
- More brains (owner: "he says I opened a new tab but he did nothing, he doesn't know when he's selected onto the browser to use the hotkeys") + "I, Oliver, made him and he obeys me"
- Fix: "he can't even take simple requests like stand down, he's straight up ignoring me"
- Fix: "you broke him" (screenshots: "I cannot open Edge or any browser", "I cannot directly navigate to kbb.com", "Pause the video and open spotify" -> "Sorry, I wasn't able to do that")
- Polish: simple commands (owner: "Nova is still struggling to answer me on simple commands")
- Nova's own browser (owner: "open kelley blue book kbb.com and search for a car, and it can effortlessly navigate the browser")
- Fix: "it's barely responding, I tell it to do something and it doesn't do it"
- Self-learning (owner: "if I tell him to open a website and he does it wrong I want to be able to correct him, like a self learning AI")
- HUD overhaul 2: the dashboard (owner: "make his bubble better looking, show what he's doing on the dashboard, kind of controlled by Nova")
- Polish pass 3 (owner: "he keeps saying I'm playing this on Spotify but nothing is happening, and that he played and full screened the video but isn't doing anything")
- Polish pass 2 (owner: "he doesn't really allow me to interrupt anymore and he talks a ton instead of just doing")
- HUD overhaul (owner: "make it look a lot better, it's poorly made now")
- Voice lock (owner's pick from the menu)
- Phase 8 — Unique features (owner picked 3 of my ideas)

## Recent sessions (newest last)
- Phone voice + PC control with Yes/No on the phone; orb on the phone.
- Security pass (phone access, Claude hand-off).
- Phone bugs: `<context>` leaking, reminders unknown to the model.
- "A real assistant": memories only when relevant, every sentence checked for made-up actions, refusals answered by Claude.
- Nova's own eyes (Windows OCR) and click by visible text.
- COM thread fix ("Cannot change thread mode").
- Whole jobs in the background with live status; paste instead of jumbled typing; `write_document`.
- Follow-ups go back to Claude; interruptible announcements; honest Ctrl+V.
- PC actions never end at the text-only Claude.
- Seamless PC control: screen watch + window arranging, then polish.
- Startup fixes: blank PowerShell, renumbered mic.
- Long jobs: out-of-steps carry-on; sign-in error detection; jobs with extra instructions.
- This Obsidian vault.

Related: [[Status and testing]] · [[Roadmap]]
