---
tags: [nova, howto, config]
---
# Configuration and data

## Config
- Defaults: `config/config.yaml`. **Your changes go in `config/local.yaml`** (git-ignored, deep-merged, never touched by updates).
- Useful settings:
  - `voice.input_device` / `output_device`: use a **name** ("fifine", "Logitech") or `null` for Windows' default, not a number.
  - `voice.mode`: `wake` (default), `open_mic`, `ptt`.
  - `voice.ack_sound: false`: turns off the tick.
  - `brain.local.model`: the Ollama model.
  - `brain.agent`: `enabled`, `max_turns`, `task_max_turns` (250), `task_timeout_s` (1800), `task_runs` (4).
  - `screen_watch`: `enabled`, `interval_s`.
  - `phone.pc_control`: `ask` / `off`.
  - `hud.enabled`, `safety.risk_overrides`, `tools.app_aliases`.
  - `assistant.name`, `owner_name: Oliver`, `address_user_as`.
- Secrets: env/.env, then **Windows Credential Manager** (`python -m assistant secrets set NAME`).

## Data on the PC (`data\`, git-ignored)
| File | What |
|---|---|
| `logs\nova.log`, `logs\supervisor.log` | output (send when something breaks) |
| `audit.jsonl` | every tool call |
| `reminders.json` | timers and reminders |
| `memory.db` | memories |
| `lessons.json` | learned corrections and shortcuts |
| `learned.json` | teach-by-showing routines |
| `voice.json` | voice designer settings |
| `voiceprint.json` | voice lock prints (numbers, no audio) |
| `phone.json` | phone devices |
| `blocked-sites.json` | banned websites |
| `location.json` | city for the weather |
| `hud-url.txt`, `hud-browser\`, `nova-browser\` | window and Nova's-browser profiles |
| `agent\` | Claude's empty workspace and run logs |
| Documents\Nova | what Claude writes with `write_document` |
