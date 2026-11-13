---
tags: [nova, architecture, brain]
---
# Local brain

`assistant/brain/local.py`, class `LocalBrain`. Model: `qwen3:4b-instruct-2507-q4_K_M` in Ollama.

## What it does
- Streams the model's reply. `ThinkFilter` hides reasoning; `ContextFilter` hides any made-up tags (`<context>`, `<monitor>`...) the model copies into replies.
- Each turn gets a `<context>` block: time, the window in front, Nova's browser, what's playing, timers and reminders set (`_upcoming`), the background job, a problem spotted on screen, and **only the memories relevant to this request** (`MemoryStore.for_turn`).
- Warm-up with **exactly** the chat options: different Ollama options (e.g. `num_ctx`) reload the model, which takes seconds.
- Everything sent goes through `brain/fit.py` to fit the 8,192-token window.

## Honesty guards
- **Nudge:** when the model promises or claims an action without calling a tool, it's told to actually do it. If it still doesn't, the reply becomes "I wasn't able to do that".
- **`_rescued`**: checks every sentence.
  - `_DONE_CLAIM` ("Opened", "Pasted", "Already done") or `_STATE_CLAIM` ("Firefox is open") with no tool run → held back.
  - `_DOING_CLAIM` later in the reply with no tool → dropped.
  - `_CANT` / `_refuses` to an instruction → [[Work it out]].
  - `_HELP_REFUSAL` ("I can't help with that") → `_answer_instead`: Claude answers. Oliver: "I want to be able to do anything with Nova".
- `_wrong_target`: it may not close or minimise "this" unless you said this/it/that ("close Spotify" once closed Firefox).
- `_prohibition`: "don't ..." / "stop ..." messages never act. About a website, they ban it.
- `_complaint`: "you didn't open it" → past tense to base verb, then it's run.
- `_CORRECTION`: "no, I meant X" → X.

## Keep the tool list short
At 54 tools the 4B model stopped calling tools ("you broke him"). Tools you say directly go on the [[Fast path]] and into `tools.MODEL_HIDDEN`.

Related: [[How Nova thinks]] · [[Request flow]] · [[Lessons learned]]
