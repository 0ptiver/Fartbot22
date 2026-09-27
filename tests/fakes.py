"""A scripted stand-in for anthropic.AsyncAnthropic built from real SDK types."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from anthropic.types import Message


def message(content: list[dict[str, Any]], stop_reason: str = "end_turn") -> Message:
    return Message.model_validate({
        "id": "msg_test", "type": "message", "role": "assistant", "model": "fake",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    })


def text_msg(text: str) -> Message:
    return message([{"type": "text", "text": text}])


def tool_msg(name: str, args: dict[str, Any], tid: str = "toolu_1", text: str = "") -> Message:
    content = [{"type": "text", "text": text}] if text else []
    content.append({"type": "tool_use", "id": tid, "name": name, "input": args})
    return message(content, "tool_use")


class FakeStream:
    def __init__(self, final: Message, delay: float = 0.0):
        self.final = final
        self.delay = delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _events(self):
        for block in self.final.content:
            if block.type == "text":
                # Stream text word by word, like the SDK's `text` events.
                for i, word in enumerate(block.text.split(" ")):
                    if self.delay:
                        await asyncio.sleep(self.delay)
                    yield SimpleNamespace(type="text", text=(" " if i else "") + word)

    def __aiter__(self):
        return self._events()

    async def get_final_message(self) -> Message:
        return self.final


class FakeMessages:
    def __init__(self, script: list[Any], delay: float = 0.0):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.delay = delay

    def stream(self, **kwargs):
        # Snapshot messages: the brain mutates the list after the call.
        import copy
        self.calls.append(copy.deepcopy(kwargs))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeStream(item, self.delay)


class FakeClient:
    def __init__(self, script: list[Any], expert_script: list[Any] | None = None, delay: float = 0.0):
        self.messages = FakeMessages(script, delay)
        self.beta = SimpleNamespace(messages=FakeMessages(expert_script or []))
