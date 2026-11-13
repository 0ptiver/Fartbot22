# Nova: handoff to the next session

## What this is
**Nova** is a voice-first, JARVIS-style assistant on Oliver's Windows 11 gaming laptop. It's a British butler that calls him "sir".
- Repo: `0ptiver/Fartbot22`, branch **`claude/jarvis-voice-assistant-lhnfza`** (commit and push there after every milestone).
- On the PC: `C:\Users\patto\Fartbot22`. Obsidian vault: `C:\Users\patto\ObsidianClaude\Nova`.
- **Read first:** `CLAUDE.md` (the full handoff: architecture, lessons learned, status, conventions), then `PLAN.md` and `README.md`.
- The Obsidian notes (`vault/Nova/`, 34 linked notes, index "Nova Home") are the same knowledge in readable form. The zip that came with this handoff contains them.

## The owner (Oliver)
- Not a developer. Give plain words, copy-paste PowerShell commands, and ask for screenshots plus `data\logs\nova.log`.
- Tests on the real PC; you can't run anything on his hardware. **Always be honest about what was and wasn't tested there.**
- Update command: `powershell -ExecutionPolicy Bypass -File scripts\update.ps1`. **Never tell him to run `pip install`** (it puts the CPU voice engine back).
- Normal start: tray (`.venv\Scripts\pythonw -m assistant background`). Foreground for debugging: `.venv\Scripts\python -m assistant voice` (quit the tray one first).
- Cares about privacy and security, and hates chatter and made-up "done" claims. Free or subscription only (Claude Pro, no API fees).
- Each hand-back: what changed in plain words, the update command, what to try, what to send back if it fails.

## Work done in the last session (all pushed; none yet confirmed on the PC)
1. **Seamless PC control:** screen watch for error boxes and frozen apps ("fix it", "what does it say", "ignore it", "close it"), and window arranging (snap left/right, other screen / screen N, side by side, undo). Polished.
2. **Blank PowerShell looked frozen:** `assistant background` now prints that Nova is running in the tray.
3. **Nova wouldn't start:** `input_device: 3` in his `config/local.yaml` became the speakers after Windows renumbered devices. `audio.pick_device` now falls back to the default mic; the doctor checks the configured mic. He should set the mic by name (`fifine` / `Logitech` / `null`).
4. **"Claude Code failed: exit 1" on long jobs:** that was Claude reaching `--max-turns` (exit 1; the reason is only in the JSON `errors`/`subtype`, checked with the real CLI). `expert._parse` reads it and raises `OutOfSteps`; background jobs carry on in a fresh run with `Job.carry_on()`, up to `brain.agent.task_runs` = 4.
5. **Sign-in detection:** only Claude Code's own messages count as signed out (incl. "Failed to authenticate: OAuth session expired"), with fix-it steps. His sign-in **had really expired**: he was told to run `claude` → `/login` → "Claude account with subscription", then test with `claude -p "say hi"`. **Not confirmed fixed yet.**
6. **Whole-job requests with extra instructions** (", don't press finish", ", start at question 22") and "use Claude to <whole job>" now run as background jobs (`_whole_job`, `_JOB_EXTRAS`).
7. **Obsidian vault:** `vault/Nova/`, `scripts/vault.ps1` (copies into `%USERPROFILE%\ObsidianClaude\Nova`), and `update.ps1` runs it when that folder exists. Folder sharing via Remote Control didn't work: no folders were listed for the cloud session. **Not confirmed that the notes reached his vault.**

Tests: 1,039 pass (`timeout 200 .venv/bin/python -m pytest -q`), pyflakes clean.

## Open items / first things to ask
- Did `claude -p "say hi"` work after `/login`? Did the update run cleanly, and did Nova start in the tray?
- Did the notes appear in Obsidian (green "Copied 34 Nova notes" line)?
- Does a long background job now get past 5-10 minutes?
- Has he tried screen watch / window arranging?
- Has he set `input_device` by name?

## Lines already drawn
- **Declined:** pacing quiz answers so automation "doesn't look robotic" (detection evasion), and further quiz-specific work. General background-job reliability fixes are fine.
- Claude's hand-off never gets file tools or WebFetch (exfiltration risk). Tests must never run the real Claude CLI.
- No keyboard hooks. No model identifiers in commits. Commit messages end with the attribution lines the system gives.

## Next features (Oliver picks from the menu in CLAUDE.md)
"Clip that" + game mode; morning briefing + health alerts; Gmail + Calendar; Discord by voice, Steam sale watcher, clipboard memory, read-to-me, window layouts, vision clicking for games.
