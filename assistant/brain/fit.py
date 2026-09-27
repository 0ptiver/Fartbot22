"""Fit the conversation into the local model's context window.

The system prompt and tool list alone are ~5,200 of the model's 8,192 tokens. When a request is
longer than that, Ollama silently drops the oldest messages to make it fit, and in a long tool
loop (the browser's page listings are big) that could be the user's own request: Nova then carried
on without knowing what it had been asked (owner: "I can't use him for more than 5 minutes
without him bugging out and not knowing what he is doing"). So we decide what goes, in order:
stale <context> blocks of earlier turns, long tool results of earlier turns, whole earlier turns
(oldest first), and only then the current turn's older tool results. The current request, with
its context, is always sent."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 3.4          # Qwen's tokenizer: ~4.3 for prose, ~3.6 for JSON; err on the safe side
REPLY_TOKENS = 700             # room for the answer (spoken replies are short)
OLD_TOOL_CHARS = 300           # a tool result from an earlier turn: the gist is enough
MIN_TOOL_CHARS = 600           # never cut the current turn's results below this
_CTX = re.compile(r"<context>.*?</context>\n?", re.S)


def tokens(obj: Any) -> int:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return int(len(text) / CHARS_PER_TOKEN) + 4


def is_request(m: dict) -> bool:
    """A message the user said (not a tool result, not Nova's own nudge to itself)."""
    return m.get("role") == "user" and not m.get("nudge") and isinstance(m.get("content"), str)


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n].rstrip() + " …(shortened)"


def fit(messages: list[dict], budget: int) -> list[dict]:
    """A copy of `messages` whose estimated size is within `budget` tokens."""
    starts = [i for i, m in enumerate(messages) if is_request(m)]
    current = starts[-1] if starts else 0
    out: list[dict] = []
    for i, m in enumerate(messages):
        m = {k: v for k, v in m.items() if k != "nudge"}
        c = m.get("content")
        if i < current and isinstance(c, str):
            if m["role"] == "user":
                m["content"] = _CTX.sub("", c)            # the screen/time back then: stale, and long
            elif m["role"] == "tool":
                m["content"] = _clip(c, OLD_TOOL_CHARS)
        out.append(m)
    sizes = [tokens(m) for m in out]
    total = sum(sizes)
    if total <= budget:
        return out
    # Drop whole earlier turns, oldest first.
    first = 0
    for k in range(len(starts) - 1):
        if total <= budget:
            break
        total -= sum(sizes[first:starts[k + 1]])
        first = starts[k + 1]
    else:
        if starts and total > budget:
            total -= sum(sizes[first:current])
            first = current
    dropped = first
    out, sizes = out[first:], sizes[first:]
    # Still too long: shorten this turn's tool results, oldest first; the newest keeps the most.
    tools = [i for i, m in enumerate(out) if m.get("role") == "tool" and isinstance(m.get("content"), str)]
    for n, i in enumerate(tools):
        if total <= budget:
            break
        last = n == len(tools) - 1
        over = total - budget
        keep = max(MIN_TOOL_CHARS, len(out[i]["content"]) - int(over * CHARS_PER_TOKEN)) if last else MIN_TOOL_CHARS
        if len(out[i]["content"]) > keep:
            out[i] = {**out[i], "content": _clip(out[i]["content"], keep)}
            total += tokens(out[i]) - sizes[i]
            sizes[i] = tokens(out[i])
    if dropped:
        log.info("context: left out %d earlier message(s) to fit the model's window", dropped)
    return out
