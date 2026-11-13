---
tags: [nova, architecture, tools, security]
---
# Tools and safety

`assistant/tools/registry.py`: each tool has a JSON schema, a **risk level** (`safe` / `confirm` / `blocked`), a time limit (`TOOL_TIMEOUTS`) and is written to the audit log `data/audit.jsonl`.

## Tool groups (`assistant/tools/`)
| Module | Does |
|---|---|
| system | time, volume, open apps (`core/launch.py`, detached from Nova's console) |
| files | allowlisted folders only; deletes go to the Recycle Bin |
| timers | timers, reminders, alarms (`data/reminders.json`) |
| pc | status, lock, power (confirm), window (focus, full screen, snap, arrange), websites, keys |
| grid | voice mouse + numbers overlay on any screen; also draws subtitles |
| uia | click by name, show numbers, read the current URL |
| keyboard | type (pastes anything over 20 chars), keys (checked), dictation, terminal guard |
| video / media | browser video via Windows' media list + the player's own buttons; pause/play/next anything |
| mybrowser / browser / apps | see [[Browser control]] |
| ocr | see [[Screen reading]] |
| music | Spotify; see [[Spotify and media]] |
| watch, memory, voice, teach, routines, learn, quick, hudnav, screen, expert, web | see [[Unique features]], [[Memory]], [[Routines]] |

## Safety rules
- Risky actions (shut down, delete, send, empty the bin) need **your** yes, by voice or the HUD's Yes/No. Text from pages or the screen can never approve.
- From the phone: `phone.pc_control: ask` makes PC control show Yes/No on the phone; `registry.REMOTE_NEVER` (terminal, voice lock, dictation, teach) is always blocked.
- Every address is **fact-checked** (`tools/sitecheck.py`): lookalikes of known sites (one letter off, doubled letters, .co for .com) open the real site with a note; banned sites (`data/blocked-sites.json`) never open; unknown site names become a search. (kbbb.com, a scam lookalike of kbb.com, once opened ~50 times.)
- **COM on worker threads**: always `core.com.com_ready()` ("Cannot change thread mode" bug).
- Unexpected tool errors are said in plain words; details go to nova.log and the audit log.
- `python -m assistant tools` lists tools and risks; `python -m assistant audit 20` shows the last 20 calls.

Related: [[Lessons learned]] · [[Phone access]]
