---
tags: [nova, architecture, claude]
---
# Claude hand-off (expert)

`assistant/brain/expert.py`, `ClaudeCodeExpert`. Answers hard questions with Claude on the Pro plan through the **official Claude Code CLI**.

- Runs `claude -p` with the task on stdin, `--output-format json`, `--permission-mode dontAsk`, web search only, Read confined to an empty workspace, `--strict-mcp-config`.
- API keys are stripped from its environment, so the **subscription** is used, not paid API.
- **Never `--bare`**: it ignores the subscription login.
- Routes: "ask Claude ...", the `escalate` tool, "what's on my screen?" (vision), summarising a page.
- `escalate` **can't see or control the PC**. A PC action that ends at `escalate` goes to [[Work it out]] instead.

## Reading Claude Code's answer (`_parse`)
| What Claude Code returns | What Nova says |
|---|---|
| `subtype: error_max_turns`, exit 1, nothing on stderr | `OutOfSteps` ("ran out of steps"). A [[Background jobs]] job carries on in a fresh run |
| "Prompt is too long" / context full | `OutOfSteps` too |
| "Failed to authenticate: OAuth session expired", "run /login", "invalid API key", "OAuth token" | "Claude Code isn't signed in... run `claude`, type `/login`" |
| "usage limit" / "rate limit" | "Your Claude plan's usage limit has been reached for now" |
| anything else | "Claude Code failed: <the real reason from `result` or `errors`>" |

> [!note] Why "exit 1"
> Claude Code puts the reason in the JSON's `errors` list, not `result`. Nova used to show only "exit 1" (fixed; checked with the real CLI).

Related: [[Work it out]] · [[How Nova thinks]] · [[Troubleshooting]]
