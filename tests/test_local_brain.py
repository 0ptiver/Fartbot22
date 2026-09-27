"""LocalBrain against a scripted fake Ollama server (httpx.MockTransport)."""

import asyncio
import json

import httpx
import pytest

from assistant.brain.expert import ExpertError
from assistant.brain.llm import BrainError, TextDelta, ToolFinished, ToolStarted, TurnComplete
from assistant.brain.local import LocalBrain
from assistant.core.conversation import Conversation
from assistant.tools import build_registry
from assistant.tools.registry import AuditLog


def ndjson(*chunks):
    return "\n".join(json.dumps(c) for c in chunks) + "\n"


def text_reply(text):
    words = text.split(" ")
    chunks = [{"message": {"role": "assistant", "content": (" " if i else "") + w}, "done": False}
              for i, w in enumerate(words)]
    chunks.append({"message": {"role": "assistant", "content": ""}, "done": True,
                   "prompt_eval_count": 50, "eval_count": len(words)})
    return ndjson(*chunks)


def tool_reply(name, args, text=""):
    chunks = []
    if text:
        chunks.append({"message": {"role": "assistant", "content": text}, "done": False})
    chunks.append({"message": {"role": "assistant", "content": "",
                               "tool_calls": [{"function": {"name": name, "arguments": args}}]},
                   "done": False})
    chunks.append({"message": {"role": "assistant", "content": ""}, "done": True})
    return ndjson(*chunks)


class FakeOllama:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def handler(self, request: httpx.Request):
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        reply = self.replies.pop(0)
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, text=reply)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), base_url="http://ollama")


class FakeExpert:
    workspace = None

    def __init__(self, answer="Claude says hi.", error=None):
        self.answer, self.error, self.calls = answer, error, []

    async def ask(self, task, context="", image_path=None):
        self.calls.append((task, context, image_path))
        if self.error:
            raise self.error
        return self.answer


@pytest.fixture
def local_settings(settings):
    settings.brain.backend = "local"
    return settings


def make(settings, replies, expert=None):
    fake = FakeOllama(replies)
    reg = build_registry(settings, AuditLog(settings.safety.audit_path()))
    brain = LocalBrain(settings, reg, http=fake.client(), expert=expert or FakeExpert())
    return brain, fake


async def collect(brain, conv, text, ctx):
    return [e async for e in brain.run_turn(conv, text, ctx)]


async def test_text_turn(local_settings, ctx):
    brain, fake = make(local_settings, [text_reply("Good evening, sir.")])
    conv = Conversation()
    events = await collect(brain, conv, "hello", ctx)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Good evening, sir."
    assert isinstance(events[-1], TurnComplete) and events[-1].usage["input_tokens"] == 50
    path, body = fake.requests[0]
    assert path == "/api/chat" and body["model"] == "qwen3:4b-instruct-2507-q4_K_M" and body["think"] is False
    assert body["messages"][0]["role"] == "system" and "escalate" in body["messages"][0]["content"]
    assert {t["function"]["name"] for t in body["tools"]} >= {"get_time", "escalate", "web_search"}
    assert [m["role"] for m in conv.messages] == ["user", "assistant"]


async def test_tool_loop(local_settings, ctx):
    brain, fake = make(local_settings, [tool_reply("get_time", {}), text_reply("It is noon, sir.")])
    conv = Conversation()
    events = await collect(brain, conv, "time?", ctx)
    fin = next(e for e in events if isinstance(e, ToolFinished))
    assert fin.name == "get_time" and not fin.is_error
    assert [m["role"] for m in conv.messages] == ["user", "assistant", "tool", "assistant"]
    assert conv.messages[2]["tool_name"] == "get_time"
    assert fake.requests[1][1]["messages"][-1]["role"] == "tool"


async def test_escalation_goes_to_expert(local_settings, ctx):
    expert = FakeExpert("The best monitor is X.")
    brain, fake = make(local_settings, [
        tool_reply("escalate", {"task": "best 1440p monitor under $400"}, text="On it, sir."),
        text_reply("Claude recommends X, sir."),
    ], expert)
    conv = Conversation()
    events = await collect(brain, conv, "research monitors", ctx)
    assert expert.calls[0][0] == "best 1440p monitor under $400"
    assert conv.messages[2]["content"] == "The best monitor is X."
    assert events[-1].text == "On it, sir. Claude recommends X, sir."


async def test_expert_failure_is_reported_not_raised(local_settings, ctx):
    expert = FakeExpert(error=ExpertError("Claude Code isn't signed in."))
    brain, _ = make(local_settings, [tool_reply("escalate", {"task": "x"}), text_reply("Sorry, sir.")], expert)
    conv = Conversation()
    events = await collect(brain, conv, "hard", ctx)
    fin = next(e for e in events if isinstance(e, ToolFinished))
    assert fin.is_error and "signed in" in conv.messages[2]["content"]


async def test_string_arguments_are_parsed(local_settings, ctx):
    brain, _ = make(local_settings, [tool_reply("volume", '{"action": "explode"}'), text_reply("Hm.")])
    conv = Conversation()
    await collect(brain, conv, "vol", ctx)
    assert "Invalid arguments" in conv.messages[2]["content"]  # parsed, then schema-validated


async def test_screenshot_described_by_vision_model(local_settings, ctx, monkeypatch):
    from PIL import Image

    from assistant.tools import screen
    monkeypatch.setattr(screen, "grab_screen", lambda m=1: Image.new("RGB", (100, 50), "red"))
    vision = ndjson({"message": {"content": "A red error dialog."}, "done": True})
    brain, fake = make(local_settings, [tool_reply("look_at_screen", {"focus": "error"}), vision,
                                        text_reply("There's an error, sir.")])
    conv = Conversation()
    await collect(brain, conv, "what's on my screen", ctx)
    path, body = fake.requests[1]
    assert body["model"] == "qwen3-vl:4b" and body["messages"][0]["images"]
    assert "A red error dialog." in conv.messages[2]["content"]


async def test_think_flag_retry(local_settings, ctx):
    bad = httpx.Response(400, text='{"error":"model does not support thinking"}')
    brain, fake = make(local_settings, [bad, text_reply("Hello.")])
    events = await collect(brain, Conversation(), "hi", ctx)
    assert isinstance(events[-1], TurnComplete)
    assert "think" not in fake.requests[1][1]


async def test_missing_model_and_offline(local_settings, ctx):
    brain, _ = make(local_settings, [httpx.Response(404, text='{"error":"model not found"}')])
    conv = Conversation()
    events = await collect(brain, conv, "hi", ctx)
    assert isinstance(events[-1], BrainError) and "ollama pull" in events[-1].message
    assert conv.messages == []

    def down(request):
        raise httpx.ConnectError("refused")
    brain.http = httpx.AsyncClient(transport=httpx.MockTransport(down), base_url="http://ollama")
    events = await collect(brain, conv, "hi", ctx)
    assert "Ollama" in events[-1].message


async def test_cancel_keeps_valid_history(local_settings, ctx):
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(200):
                await asyncio.sleep(0.005)
                yield (json.dumps({"message": {"content": "word "}, "done": False}) + "\n").encode()

    brain, _ = make(local_settings, [httpx.Response(200, stream=Slow())])
    conv = Conversation()

    async def consume():
        async for _ in brain.run_turn(conv, "story", ctx):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [m["role"] for m in conv.messages] == ["user", "assistant"]
    assert conv.messages[1]["content"].endswith("[interrupted]")


def run_filter(parts, hold=False):
    from assistant.brain.local import ThinkFilter
    f = ThinkFilter(hold)
    return "".join(f.feed(p) for p in parts) + f.flush()


def test_think_filter_drops_tagged_reasoning():
    assert run_filter(["<thi", "nk>Okay the user wants", " hello.</think>", "\n\nHello, sir."]) == "Hello, sir."
    assert run_filter(["Hello", ", sir."]) == "Hello, sir."
    assert run_filter(["<think>never closed"]) == ""
    assert run_filter(["<"]) == "<"


def test_think_filter_hold_mode():
    # Thinking-only models often omit the opening tag.
    assert run_filter(["Okay, the user asked", " me...", "</think>", "Hello, sir."], hold=True) == "Hello, sir."
    assert run_filter(["Hello, sir."], hold=True) == "Hello, sir."


async def test_thinking_model_reasoning_never_spoken(local_settings, ctx):
    bad = httpx.Response(400, text='{"error":"think value \\"false\\" is not supported for this model"}')
    reply = ndjson({"message": {"content": "Okay, the user asked me to say hello. "}, "done": False},
                   {"message": {"content": "</think>\n\nHello, sir."}, "done": False},
                   {"message": {"content": ""}, "done": True})
    brain, fake = make(local_settings, [bad, reply])
    events = await collect(brain, Conversation(), "hi", ctx)
    spoken = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert spoken == "Hello, sir."


async def test_separated_thinking_field_ignored(local_settings, ctx):
    reply = ndjson({"message": {"thinking": "hmm let me think", "content": ""}, "done": False},
                   {"message": {"content": "Hello, sir."}, "done": True})
    brain, _ = make(local_settings, [reply])
    events = await collect(brain, Conversation(), "hi", ctx)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello, sir."


async def test_warm_up_matches_real_requests(local_settings, ctx):
    brain, fake = make(local_settings, [text_reply("x"), text_reply("Hello.")])
    await brain.warm_up()
    await collect(brain, Conversation(), "hi", ctx)
    (_, warm), (_, real) = fake.requests
    # Same model, context size, system prompt and tools -> no reload, prompt cache hit.
    assert warm["options"]["num_ctx"] == real["options"]["num_ctx"]
    assert warm["options"]["num_predict"] == 1
    assert warm["messages"][0] == real["messages"][0]
    assert warm["tools"] == real["tools"] and warm["model"] == real["model"]


async def test_warm_up_errors(local_settings):
    brain, _ = make(local_settings, [httpx.Response(404, text='{"error":"model not found"}')])
    with pytest.raises(Exception, match="ollama pull"):
        await brain.warm_up()


async def test_promise_without_action_is_nudged(local_settings, ctx):
    brain, fake = make(local_settings, [
        text_reply("I'll check that for you, sir."),        # says it, doesn't do it
        tool_reply("get_time", {}),                          # nudged -> calls the tool
        text_reply("It is noon."),
    ])
    conv = Conversation()
    events = await collect(brain, conv, "what time is it", ctx)
    assert any(isinstance(e, ToolFinished) and e.name == "get_time" for e in events)
    assert "did not call a tool" in fake.requests[1][1]["messages"][-1]["content"]
    assert events[-1].text == "I'll check that for you, sir. It is noon."


async def test_no_nudge_for_plain_answers(local_settings, ctx):
    brain, fake = make(local_settings, [text_reply("Good evening, sir.")])
    await collect(brain, Conversation(), "hello", ctx)
    assert len(fake.requests) == 1


async def test_ask_claude_goes_straight_to_expert(local_settings, ctx):
    expert = FakeExpert("The Dell S2721DGF.")
    brain, fake = make(local_settings, [text_reply("Claude suggests the Dell S2721DGF, sir.")], expert)
    conv = Conversation()
    conv.messages += [{"role": "user", "content": "<context>t</context>\nI play shooters"},
                      {"role": "assistant", "content": "Noted, sir."}]
    events = await collect(brain, conv, "Ask Claude what the best 1440p monitor under $400 is.", ctx)
    task, context, _ = expert.calls[0]
    assert task == "what the best 1440p monitor under $400 is."
    assert "I play shooters" in context
    assert isinstance(events[0], ToolStarted) and events[0].name == "escalate"
    assert len(fake.requests) == 1                 # only the relay round used the local model
    assert fake.requests[0][1]["messages"][-1]["role"] == "tool"


def test_ask_claude_pattern():
    from assistant.brain.local import direct_escalation
    c = Conversation()
    assert direct_escalation("Can you ask Claude to plan my week", c)["function"]["arguments"]["task"] == "plan my week"
    assert direct_escalation("have claude, summarize this", c) is not None
    assert direct_escalation("I asked Claude yesterday", c) is None
    assert direct_escalation("what time is it", c) is None
