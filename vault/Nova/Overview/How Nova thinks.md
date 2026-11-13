---
tags: [nova, overview, brain]
---
# How Nova thinks

| Job | Who does it | Cost |
|---|---|---|
| Common commands ("pause", "open Spotify", "snap Firefox left") | The [[Fast path]]: pattern matching, no model | free, instant |
| Everyday chat, PC control, quick facts | [[Local brain]]: `qwen3:4b-instruct-2507-q4_K_M` in Ollama on the GPU | free |
| "Ask Claude ...", research, writing, "what's on my screen?" | [[Claude hand-off]]: the official Claude Code CLI on the Pro plan | included in Pro |
| When Nova is stuck, or "work it out" | [[Work it out]]: Claude Code with Nova's own tools | included in Pro |
| Whole jobs ("do my homework") | [[Background jobs]]: Claude with Nova's tools, in the background | included in Pro |
| Optional paid route | Claude API (`brain.backend: anthropic`) | per token |

- Nova runs the real, unmodified `claude` program signed in with your own account. **Nova never sees or stores your Claude login.** If the sign-in expires, run `claude` then `/login` (see [[Troubleshooting]]).
- The local model is the **instruct** variant: the "thinking" variants read their reasoning out loud.
- The local model's window is 8,192 tokens and the prompt plus tools take about 5,200, so everything is squeezed to fit (`brain/fit.py`).
- The local model is small (4B). It fumbles multi-step tasks, so common commands go on the fast path and anything hard goes to Claude.

Related: [[Request flow]]
