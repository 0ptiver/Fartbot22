import asyncio

import anthropic
import httpx2
import pytest

from tests.fakes import FakeClient, text_msg, tool_msg
from assistant.brain.llm import Brain, BrainError, TextDelta, ToolFinished, ToolStarted, TurnComplete
from assistant.core.conversation import Conversation
from assistant.tools.registry import ToolContext


async def collect(brain, conv, text, ctx):
    return [ev async for ev in brain.run_turn(conv, text, ctx)]


async def test_text_turn_streams_and_caches(settings, registry, ctx):
    client = FakeClient([text_msg("Good evening, sir.")])
    brain = Brain(settings, registry, client)
    conv = Conversation()
    events = await collect(brain, conv, "hello", ctx)

    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Good evening, sir."
    done = events[-1]
    assert isinstance(done, TurnComplete) and done.text == "Good evening, sir."
    assert "first_token_ms" in done.timings

    call = client.messages.calls[0]
    assert call["model"] == settings.brain.chat_model
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["cache_control"] == {"type": "ephemeral"}
    names = [t["name"] for t in call["tools"]]
    assert "web_search" in names and "get_time" in names
    # Per-turn context lives in the user message, not the (cached) system prompt.
    assert "<context>" in call["messages"][0]["content"][0]["text"]
    assert [m["role"] for m in conv.messages] == ["user", "assistant"]


async def test_tool_loop(settings, registry, ctx):
    client = FakeClient([tool_msg("get_time", {}, text="One moment."), text_msg("It is noon.")])
    brain = Brain(settings, registry, client)
    conv = Conversation()
    events = await collect(brain, conv, "what time is it", ctx)

    kinds = [type(e) for e in events]
    assert ToolStarted in kinds and ToolFinished in kinds
    fin = next(e for e in events if isinstance(e, ToolFinished))
    assert fin.name == "get_time" and not fin.is_error
    assert events[-1].text == "One moment. It is noon."
    assert [m["role"] for m in conv.messages] == ["user", "assistant", "user", "assistant"]
    tr = conv.messages[2]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "toolu_1"
    # Second request carries the tool result.
    assert client.messages.calls[1]["messages"][2]["content"][0]["type"] == "tool_result"
    assert registry.audit.tail()[-1]["tool"] == "get_time"


async def test_tool_round_cap_forces_answer(settings, registry, ctx):
    settings.brain.max_tool_rounds = 1
    client = FakeClient([tool_msg("get_time", {}), text_msg("Done.")])
    brain = Brain(settings, registry, client)
    await collect(brain, Conversation(), "loop", ctx)
    assert "tool_choice" not in client.messages.calls[0]
    assert client.messages.calls[1]["tool_choice"] == {"type": "none"}


async def test_max_tokens_tool_input_not_run(settings, registry, ctx):
    from tests.fakes import message
    truncated = message([{"type": "tool_use", "id": "t1", "name": "volume",
                          "input": {"action": "set"}}], stop_reason="max_tokens")
    client = FakeClient([truncated, text_msg("Sorry.")])
    brain = Brain(settings, registry, client)
    conv = Conversation()
    events = await collect(brain, conv, "volume", ctx)
    assert not any(isinstance(e, ToolStarted) for e in events)
    assert conv.messages[2]["content"][0]["is_error"] is True


async def test_expert_escalation(settings, registry, ctx):
    client = FakeClient(
        [tool_msg("escalate", {"task": "prove it"}), text_msg("The expert agrees.")],
        expert_script=[text_msg("Short answer. Details.")],
    )
    brain = Brain(settings, registry, client)
    conv = Conversation()
    await collect(brain, conv, "hard question", ctx)
    expert_call = client.beta.messages.calls[0]
    assert expert_call["model"] == settings.brain.expert_model
    assert expert_call["fallbacks"] == "default"
    assert conv.messages[2]["content"][0]["content"] == "Short answer. Details."


async def test_connection_error_rolls_back(settings, registry, ctx):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    client = FakeClient([anthropic.APIConnectionError(request=req)])
    brain = Brain(settings, registry, client)
    conv = Conversation()
    events = await collect(brain, conv, "hi", ctx)
    assert isinstance(events[-1], BrainError)
    assert conv.messages == []


async def test_cancel_keeps_valid_history(settings, registry, ctx):
    long = " ".join(["word"] * 200)
    client = FakeClient([text_msg(long)], delay=0.005)
    brain = Brain(settings, registry, client)
    conv = Conversation()

    async def consume():
        async for _ in brain.run_turn(conv, "tell me a story", ctx):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [m["role"] for m in conv.messages] == ["user", "assistant"]
    assert conv.messages[1]["content"][0]["text"].endswith("[interrupted]")


def test_conversation_trim():
    conv = Conversation(max_turns=2)
    for i in range(4):
        conv.add_user(f"q{i}")
        conv.add_assistant([{"type": "tool_use", "id": f"t{i}", "name": "x", "input": {}}])
        conv.add_tool_results([{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}])
        conv.add_assistant([{"type": "text", "text": f"a{i}"}])
    conv.trim()
    assert len(conv.messages) == 8
    assert conv.messages[0]["content"][0]["text"] == "q2"
