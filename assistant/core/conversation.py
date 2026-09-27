"""Rolling conversation history that always stays valid for the Messages API."""

from __future__ import annotations

from typing import Any


def _is_turn_start(msg: dict[str, Any]) -> bool:
    """A user message that isn't just tool results starts a new user turn."""
    if msg["role"] != "user" or msg.get("nudge"):
        return False
    content = msg["content"]
    if isinstance(content, str):
        return True
    return not all(b.get("type") == "tool_result" for b in content)


class Conversation:
    def __init__(self, max_turns: int = 20):
        self.max_turns = max_turns
        self.messages: list[dict[str, Any]] = []

    def add_user(self, text: str, context: str | None = None) -> None:
        blocks: list[dict[str, Any]] = []
        if context:
            blocks.append({"type": "text", "text": context})
        blocks.append({"type": "text", "text": text})
        self.messages.append({"role": "user", "content": blocks})

    def add_assistant(self, content: list[dict[str, Any]]) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def add_tool_results(self, results: list[dict[str, Any]]) -> None:
        self.messages.append({"role": "user", "content": results})

    def checkpoint(self) -> int:
        return len(self.messages)

    def rollback(self, checkpoint: int) -> None:
        del self.messages[checkpoint:]

    def trim(self) -> None:
        """Keep only the last `max_turns` user turns (cutting on turn boundaries)."""
        starts = [i for i, m in enumerate(self.messages) if _is_turn_start(m)]
        if len(starts) > self.max_turns:
            del self.messages[: starts[-self.max_turns]]

    def clear(self) -> None:
        self.messages.clear()
