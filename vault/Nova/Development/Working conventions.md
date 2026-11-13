---
tags: [nova, dev]
---
# Working conventions

- Keep PLAN.md checklists updated. Commit messages explain the "why". End commit messages with the attribution lines given by the system.
- Write tests with fakes for everything, and reproduce owner-reported bugs as tests (test names/docstrings cite the "owner's case").
- Be honest about what was and wasn't tested on real hardware. Say it in every hand-back to the owner.
- Each hand-back to the owner: what changed in plain words, the update command, what to try, and what to send back if it fails.
- Branch `claude/jarvis-voice-assistant-lhnfza`: commit and push after every milestone.
- No model identifiers in commits or code.
- Hand-backs to Oliver: plain words, copy-paste commands, the update command, what to try, what to send back if it fails, and honesty about what was tested on real hardware.
- Use they/them for Oliver in written notes unless stated otherwise.
- Keep this vault in step: notes live in the repo under `vault/Nova/`; `scripts\vault.ps1` copies them into Obsidian.
