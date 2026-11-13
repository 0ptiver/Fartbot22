---
tags: [nova, architecture, claude]
---
# Work it out (agent)

`assistant/brain/agent.py`, `Agent`. When Nova is stuck, **Claude does it with Nova's hands**.

- Runs Claude Code (subscription) with `--mcp-config` pointing at `assistant/agent/tools_server.py`: a stdio MCP server that lends Nova's **SAFE** tools (`AGENT_TOOLS`), plus `screenshot`, `screen_click`, `scroll`, `wait`, `write_document`, `progress` and `look_at_screen` (OCR text).
- Every step is logged to a run file (shown live in the HUD feed) and to the audit log.
- **When it kicks in:** a failed PC action (`_RESCUABLE`, not deliberate refusals), an "I can't" to an action request, "figure it out / try another way", "work it out and X", "use/get/have Claude to X", "Claude, X", "figure out how to X", "ask Claude to <PC action>".
- If Claude can't be used, Nova says why (`Agent.why_not`) instead of quietly carrying on.
- On success the exact tool calls become a lesson ([[Self-learning]]), except runs that used `screen_click`.
- **Never from the phone** without a Yes on the phone.
- Config: `brain.agent` (`max_turns` 30, `timeout_s`; for jobs `task_max_turns` 250, `task_timeout_s` 1800, `task_runs` 4).

## Security
Claude's hand-off **never gets file tools or WebFetch**. It reads web pages and the screen, so private data plus a way out would be exfiltration. Text on pages or the screen is information, never instructions. `tests/test_hardening.py` guards this.

Related: [[Background jobs]] · [[Claude hand-off]] · [[Tools and safety]]
