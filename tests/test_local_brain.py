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
    events = await collect(brain, conv, "tell me something interesting", ctx)
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
    # "On it, sir." before a tool call is dropped: the voice loop says its own short filler for
    # slow tools, then the answer (owner: "he talks a ton instead of just doing").
    assert events[-1].text == "Claude recommends X, sir."


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
    local_settings.brain.local.vision = "ollama"

    from assistant.tools import screen
    monkeypatch.setattr(screen, "grab_screen", lambda m=1: Image.new("RGB", (100, 50), "red"))
    vision = ndjson({"message": {"content": "A red error dialog."}, "done": True})
    brain, fake = make(local_settings, [tool_reply("look_at_screen", {"focus": "error"}), vision,
                                        text_reply("There's an error, sir.")])
    conv = Conversation()
    await collect(brain, conv, "is there anything weird going on with my monitor", ctx)
    path, body = fake.requests[1]
    assert body["model"] == "qwen3-vl:4b" and body["messages"][0]["images"]
    assert "A red error dialog." in conv.messages[2]["content"]


async def test_think_flag_retry(local_settings, ctx):
    bad = httpx.Response(400, text='{"error":"model does not support thinking"}')
    brain, fake = make(local_settings, [bad, text_reply("Hello.")])
    events = await collect(brain, Conversation(), "tell me something interesting", ctx)
    assert isinstance(events[-1], TurnComplete)
    assert "think" not in fake.requests[1][1]


async def test_missing_model_and_offline(local_settings, ctx):
    brain, _ = make(local_settings, [httpx.Response(404, text='{"error":"model not found"}')])
    conv = Conversation()
    events = await collect(brain, conv, "tell me something interesting", ctx)
    assert isinstance(events[-1], BrainError) and "ollama pull" in events[-1].message
    assert conv.messages == []

    def down(request):
        raise httpx.ConnectError("refused")
    brain.http = httpx.AsyncClient(transport=httpx.MockTransport(down), base_url="http://ollama")
    events = await collect(brain, conv, "tell me something interesting", ctx)
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
    events = await collect(brain, Conversation(), "tell me something interesting", ctx)
    spoken = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert spoken == "Hello, sir."


async def test_separated_thinking_field_ignored(local_settings, ctx):
    reply = ndjson({"message": {"thinking": "hmm let me think", "content": ""}, "done": False},
                   {"message": {"content": "Hello, sir."}, "done": True})
    brain, _ = make(local_settings, [reply])
    events = await collect(brain, Conversation(), "tell me something interesting", ctx)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello, sir."


async def test_warm_up_matches_real_requests(local_settings, ctx):
    brain, fake = make(local_settings, [text_reply("x"), text_reply("Hello.")])
    await brain.warm_up()
    await collect(brain, Conversation(), "tell me something interesting", ctx)
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
    events = await collect(brain, conv, "how late is it", ctx)
    assert any(isinstance(e, ToolFinished) and e.name == "get_time" for e in events)
    assert "did not call a tool" in fake.requests[1][1]["messages"][-1]["content"]
    # The empty promise isn't read out: only what actually happened.
    assert events[-1].text == "It is noon."


async def test_no_nudge_for_plain_answers(local_settings, ctx):
    brain, fake = make(local_settings, [text_reply("Good evening, sir.")])
    await collect(brain, Conversation(), "tell me something interesting", ctx)
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


def test_shrink_jpeg():
    import base64, io
    from PIL import Image
    from assistant.brain.local import shrink_jpeg
    buf = io.BytesIO()
    Image.new("RGB", (1568, 882), "red").save(buf, format="JPEG")
    small = shrink_jpeg(base64.b64encode(buf.getvalue()).decode(), 1024)
    assert max(Image.open(io.BytesIO(base64.b64decode(small))).size) == 1024
    tiny = base64.b64encode(buf.getvalue()).decode()
    assert shrink_jpeg(tiny, 4000) == tiny


async def test_screenshot_described_by_claude(local_settings, ctx, monkeypatch, tmp_path):
    from PIL import Image

    from assistant.tools import screen
    monkeypatch.setattr(screen, "grab_screen", lambda m=1: Image.new("RGB", (100, 50), "red"))
    local_settings.brain.local.vision = "claude_code"
    seen = {}

    class ImgExpert(FakeExpert):
        workspace = tmp_path / "ws"

        async def ask(self, task, context="", image_path=None):
            seen["exists"] = image_path is not None and image_path.exists()
            seen["inside"] = image_path.resolve().is_relative_to(self.workspace.resolve())
            return "A red error dialog."

    brain, fake = make(local_settings, [tool_reply("look_at_screen", {}), text_reply("An error, sir.")],
                       ImgExpert())
    conv = Conversation()
    await collect(brain, conv, "is there anything weird going on with my monitor", ctx)
    assert seen == {"exists": True, "inside": True}
    assert "A red error dialog." in conv.messages[2]["content"]
    assert len(fake.requests) == 2          # no local vision model call
    assert not list((tmp_path / "ws").rglob("*.jpg"))   # screenshot deleted afterwards


async def test_fast_command_skips_the_model(local_settings, ctx, monkeypatch):
    calls = []

    async def fake_now_playing(args, c):
        calls.append(args)
        return "Playing One Dance by Drake."
    brain, fake = make(local_settings, [])
    brain.registry.get("now_playing").handler = fake_now_playing
    conv = Conversation()
    events = await collect(brain, conv, "I'm playing a song, tell me what's playing", ctx)
    assert fake.requests == []                                   # no model call at all
    assert calls == [{}]
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Playing One Dance by Drake."
    assert [m["role"] for m in conv.messages] == ["user", "assistant"]
    assert isinstance(events[-1], TurnComplete)


async def test_fast_commands_can_be_disabled(local_settings, ctx):
    local_settings.brain.local.fast_commands = False
    brain, fake = make(local_settings, [text_reply("Paused, sir.")])
    await collect(brain, Conversation(), "pause", ctx)
    assert len(fake.requests) == 1


OWNERS_CLAIM = ("Unpausing YouTube and minimizing the PowerShell window is done. "
                "The video is now fullscreen, sir.")


async def test_false_done_claim_is_nudged_into_acting(local_settings, ctx):
    """Owner's case: three actions reported done, no tool called."""
    brain, fake = make(local_settings, [
        text_reply(OWNERS_CLAIM),
        tool_reply("get_time", {}),                       # stands in for the real tools
        text_reply("All three are done now, sir."),
    ])
    events = await collect(brain, Conversation(),
                           "Can you actually unpause it and minimize the PowerShell window?", ctx)
    assert "nothing happened" in fake.requests[1][1]["messages"][-1]["content"]
    assert any(isinstance(e, ToolFinished) for e in events)


async def test_ignored_nudge_is_not_said_twice(local_settings, ctx):
    brain, fake = make(local_settings, [text_reply(OWNERS_CLAIM), text_reply(
        "I will now unpause YouTube and minimize the PowerShell window. The video will be fullscreen shortly.")])
    events = await collect(brain, Conversation(), "unpause it and minimize the PowerShell window", ctx)
    said = "".join(e.text for e in events if isinstance(e, TextDelta))
    # The false "done" is never read out now (owner: "he keeps saying he did it but didn't").
    assert said == "Sorry, sir, I wasn't able to do that."
    assert said.count("I will now") == 0


async def test_an_untried_i_cant_is_not_the_answer(local_settings, ctx):
    """Owner: "if I tell it to do something then it's not gonna tell me oh sir I can't do that".
    The model never looked for Discord: its "can't" isn't relayed (Claude takes it on when it's on;
    here it's off, so Nova says plainly it wasn't able)."""
    brain, fake = make(local_settings, [text_reply("Discord is now minimized, sir."),
                                        text_reply("I can't find a Discord window, sir.")])
    events = await collect(brain, Conversation(), "minimize discord", ctx)
    assert events[-1].text.endswith("I wasn't able to do that.")


async def test_answers_mentioning_closed_are_not_claims(local_settings, ctx):
    brain, fake = make(local_settings, [text_reply("It's closed on Sundays, sir.")])
    await collect(brain, Conversation(), "is the shop open on sundays?", ctx)
    assert len(fake.requests) == 1


async def test_punctuation_tokens_are_not_spaced(local_settings, ctx):
    chunks = [{"message": {"role": "assistant", "content": c}, "done": False} for c in ["Noon", ",", " sir", "."]]
    chunks.append({"message": {"role": "assistant", "content": ""}, "done": True})
    brain, fake = make(local_settings, [ndjson(*chunks)])
    events = await collect(brain, Conversation(), "how late is it", ctx)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Noon, sir."


async def test_preamble_before_a_tool_is_not_said(local_settings, ctx):
    """Owner: "he talks a ton instead of just doing". "Certainly, sir. I'll check the time."
    followed by a tool call isn't read out: only the result is."""
    brain, fake = make(local_settings, [
        tool_reply("get_time", {}, text="Certainly, sir. I'll check the time for you."),
        text_reply("It's noon, sir."),
    ])
    events = await collect(brain, Conversation(), "how late is it", ctx)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "It's noon, sir."


async def test_an_answer_that_starts_like_a_preamble_is_still_said(local_settings, ctx):
    brain, fake = make(local_settings, [text_reply("Certainly, sir. Paris is the capital of France.")])
    events = await collect(brain, Conversation(), "capital of france?", ctx)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == \
        "Certainly, sir. Paris is the capital of France."


async def test_normal_answers_stream_straight_away(local_settings, ctx):
    brain, fake = make(local_settings, [text_reply("Paris is the capital, sir.")])
    events = await collect(brain, Conversation(), "capital of france?", ctx)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Paris is the capital, sir."


@pytest.mark.parametrize("said,asked", [
    ("I played and full screened the video, sir.", "play and full screen the video"),
    ("Playing Blinding Lights on Spotify, sir.", "put on blinding lights"),
    ("Blinding Lights is now playing.", "put on blinding lights"),
    ("I opened Discord.", "open discord please"),
])
def test_claims_without_a_tool_are_caught(said, asked):
    """Owner: "he keeps saying I played and full screened the video but he isn't doing anything"."""
    from assistant.brain.local import _acts_without_tools
    assert _acts_without_tools(said, asked)


def test_answers_are_not_claims():
    from assistant.brain.local import _acts_without_tools
    assert not _acts_without_tools("Playing football is fun.", "do you like sports?")
    assert not _acts_without_tools("The capital is Paris.", "what is the capital of france")


def test_a_copied_context_block_is_never_shown_or_said():
    """Owner's case on the phone: the reply began with Nova's whole <context> block (time, the
    window in front, remembered facts), then the actual answer."""
    from assistant.brain.local import ContextFilter
    leaked = ("<context>\ntime: Monday 28 September 2026, 09:14\nwindow in front: Windows PowerShell\n"
              "- I'm 6'3 and 170lbs\n</context>\nYou have a junk job at 10, sir.")
    for size in (1, 2, 3, 7, len(leaked)):              # however the stream happens to be cut up
        f = ContextFilter()
        out = "".join(f.feed(leaked[i:i + size]) for i in range(0, len(leaked), size)) + f.flush()
        assert out == "You have a junk job at 10, sir.", size
    f = ContextFilter()                                  # ordinary text with a "<" passes untouched
    assert f.feed("3 < 5 and <b>bold</b>") + f.flush() == "3 < 5 and <b>bold</b>"
    f = ContextFilter()                                  # never closed: none of it is said
    assert f.feed("Sure. <context>\ntime: 9:14\n- my height") + f.flush() == "Sure. "


async def test_the_model_is_told_the_reminders_that_are_set(local_settings, ctx, tmp_path):
    """Owner's case: "I literally see it right above, I see junk job at 10am" (Up next)."""
    import time as _t
    from assistant.core.scheduler import Scheduler
    sched = Scheduler(tmp_path / "rem.json", local_settings.assistant.timezone)
    sched.add("reminder", _t.time() + 45 * 60 + 30, "junk job at 10am")
    ctx.services["scheduler"] = sched
    brain, fake = make(local_settings, [text_reply(
        "<context> time: now </context> Yes sir, your junk job is at 10.")])
    events = await collect(brain, Conversation(), "do I have anything coming up", ctx)
    said = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert said == "Yes sir, your junk job is at 10." and "<context>" not in events[-1].text
    sent = fake.requests[0][1]["messages"][-1]["content"]
    assert "reminder 'junk job at 10am'" in sent and "(in 45 min)" in sent


FACTS = ["I live in Kansas City, Missouri and run a junk removal business called Trash Teens",
         "My dog's name is Sparky", "I have an i9 with 32 processors and 24 cores and a 5070 RTX",
         "When I tell you to open my business, open the url business.facebook.com on firefox",
         "I'm 6'3 and 170lbs"]


def test_only_the_memories_a_request_is_about_reach_the_model(tmp_path):
    """Owner's case: "Are you sure you said it?" got his town, business, dog and PC read out."""
    from assistant.core.memory import MemoryStore
    store = MemoryStore(tmp_path / "m.db")
    for f in FACTS:
        store.add(f)
    assert store.for_turn("Are you sure you said it?") == []
    assert store.for_turn("what's my dog called") == ["My dog's name is Sparky"]
    assert store.for_turn("hey nova how are you doing") == []


async def test_a_made_up_action_at_the_end_of_an_answer_is_dropped(local_settings, ctx):
    """Owner's case: the reply ended "Opening business.facebook.com in your browser, sir." and
    nothing opened."""
    brain, _ = make(local_settings, [text_reply(
        "Yes sir, I said your dog is Sparky. Opening business.facebook.com in your browser, sir.")])
    conv = Conversation()
    events = await collect(brain, conv, "Are you sure you said it?", ctx)
    said = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert said.strip() == "Yes sir, I said your dog is Sparky." and "Opening" not in events[-1].text
    assert "Opening" not in conv.messages[-1]["content"]


async def test_an_answer_that_is_only_a_made_up_action_says_so_honestly(local_settings, ctx):
    brain, _ = make(local_settings, [text_reply("Opening business.facebook.com in your browser, sir.")])
    events = await collect(brain, Conversation(), "tell me a joke about cats", ctx)
    said = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "Opening" not in said and "haven't actually done that" in said


async def test_real_actions_still_say_what_they_did(local_settings, ctx):
    brain, _ = make(local_settings, [tool_reply("get_time", {}), text_reply("It's noon. Playing nothing, sir.")])
    events = await collect(brain, Conversation(), "time?", ctx)
    assert "Playing nothing" in "".join(e.text for e in events if isinstance(e, TextDelta))


async def test_a_shortcut_saved_as_a_memory_becomes_a_real_shortcut(local_settings, ctx, tmp_path):
    """Owner's case: "When I tell you to open my business, open the url business.facebook.com on
    firefox" sat in memory, got read out, and Nova said it was opening the site."""
    from assistant.brain import lessons as L
    from assistant.core.memory import MemoryStore
    store = MemoryStore(tmp_path / "m.db")
    for f in FACTS:
        store.add(f)
    ctx.services["memory"] = store
    brain, _ = make(local_settings, [text_reply("Fine, sir.")])
    await collect(brain, Conversation(), "how are things going today", ctx)
    assert not any("business.facebook" in m.text for m in store.all()) and len(store.all()) == 4
    lesson = L.get_lessons().match("open my business")
    assert lesson and "business.facebook.com" in lesson.means
