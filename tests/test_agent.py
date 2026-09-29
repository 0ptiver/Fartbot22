"""Owner (chose automatic): "it keeps saying I can't do this and that ... give it all of the tools
... think and navigate when something goes wrong ... self learn ... so it gets it right in the
future". When Nova is stuck, Claude works it out with Nova's SAFE tools; what worked is learned."""

import asyncio
import json
import os
import stat
import sys
import textwrap

import pytest

from assistant.agent import tools_server as TS
from assistant.brain import agent as A
from assistant.brain import lessons as L
from assistant.brain.llm import TextDelta, ToolFinished, ToolStarted
from assistant.core.conversation import Conversation
from assistant.tools.registry import ToolContext
from tests.test_local_brain import collect, make, text_reply, tool_reply


def said(events):
    return "".join(e.text for e in events if isinstance(e, TextDelta))


# --- Nova's tools, lent to Claude -------------------------------------------------------------
async def test_only_safe_tools_are_lent(settings, registry):
    tools = TS.NovaTools(settings, registry)
    names = {t["name"] for t in tools.list()}
    assert {"my_browser", "app", "video", "window", "screenshot", "screen_click"} <= names
    assert not names & {"power", "delete_file", "move_file", "empty_recycle_bin", "forget", "block_site", "teach"}
    reply = await TS.handle(tools, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "power", "arguments": {"action": "shutdown"}}})
    assert reply["result"]["isError"]


async def test_the_bridge_speaks_mcp_and_logs_each_step(settings, registry, tmp_path):
    log = tmp_path / "run.jsonl"
    tools = TS.NovaTools(settings, registry, log_path=str(log))
    init = await TS.handle(tools, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["protocolVersion"] == "2025-06-18" and "tools" in init["result"]["capabilities"]
    assert await TS.handle(tools, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    res = await TS.handle(tools, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": "calculate", "arguments": {"expression": "17 times 23"}}})
    assert res["result"] == {"content": [{"type": "text", "text": "That's 391."}], "isError": False}
    entry = json.loads(log.read_text().splitlines()[0])
    assert entry["tool"] == "calculate" and entry["ok"] and entry["result"] == "That's 391."


async def test_typing_into_a_terminal_is_refused_with_nobody_to_ask(settings, registry, monkeypatch):
    from assistant.tools import keyboard as K, pc
    monkeypatch.setattr(pc, "WINDOWS", type("W", (), {"active": lambda s: pc.Win(6, "Windows PowerShell", "WindowsTerminal.exe"),
                                                       "list": lambda s: [], "foreground": lambda s: 6})())
    typed = []
    monkeypatch.setattr(K, "KEYBOARD", type("K", (), {"type": lambda s, t: typed.append(t), "combo": lambda s, v: None})())
    tools = TS.NovaTools(settings, registry)
    content, ok = await tools.call("type_text", {"text": "rm -rf ~"})
    assert not ok and typed == []


# --- running Claude (a pretend claude.exe) --------------------------------------------------------
def fake_claude(tmp_path, steps, final):
    """A stand-in for the Claude Code CLI: logs the steps where the bridge would, then answers."""
    script = tmp_path / "claude"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, sys
        args = sys.argv
        cfg = json.load(open(args[args.index("--mcp-config") + 1]))
        log = cfg["mcpServers"]["nova"]["env"]["NOVA_AGENT_LOG"]
        sys.stdin.read()
        with open(log, "a") as f:
            for s in {steps!r}:
                f.write(json.dumps(s) + "\\n")
        print(json.dumps({{"type": "result", "is_error": False, "result": {final!r}}}))
        """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def agent_on(local_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(A.Agent, "available", lambda self: True)
    monkeypatch.setattr(A, "ROOT", tmp_path)
    return local_settings


@pytest.mark.skipif(os.name == "nt", reason="shebang script")
async def test_agent_run_reads_steps_and_verdict(agent_on, tmp_path):
    steps = [{"tool": "screenshot", "args": {}, "ok": True, "ms": 5, "result": "Monitor 1"},
             {"tool": "my_browser", "args": {"action": "click", "text": "Instagram"}, "ok": True, "ms": 300, "result": "Clicked Instagram."}]
    agent_on.brain.expert.claude_code.command = fake_claude(tmp_path, steps, "Looked, then clicked it.\nDONE: Opened Instagram.")
    seen = []
    res = await A.Agent(agent_on).run("press the instagram", on_step=seen.append)
    assert res.ok and res.say == "Opened Instagram." and [s["tool"] for s in seen] == ["screenshot", "my_browser"]
    assert A.learnable(res.steps) == [{"tool": "my_browser", "args": {"action": "click", "text": "Instagram"}}]


def test_verdicts_and_what_is_worth_learning():
    assert A._verdict("thinking...\nFAILED: The page needs a login.") == (False, "The page needs a login.")
    assert A._verdict("**DONE: Opened it.**") == (True, "Opened it.")
    assert A.learnable([{"tool": "screen_click", "args": {}, "ok": True}, {"tool": "app", "args": {"action": "click"}, "ok": True}]) == []
    assert A.learnable([{"tool": "app", "args": {"action": "look"}, "ok": True},
                        {"tool": "app", "args": {"action": "click", "target": "Send"}, "ok": True},
                        {"tool": "app", "args": {"action": "click", "target": "X"}, "ok": False}]) == \
        [{"tool": "app", "args": {"action": "click", "target": "Send"}}]


# --- Nova handing over when stuck -----------------------------------------------------------------
class FakeAgent:
    def __init__(self, ok=True, say="Opened Instagram.", steps=None):
        self.ok, self.say, self.calls = ok, say, []
        self.steps = steps if steps is not None else [
            {"tool": "screenshot", "args": {}, "ok": True, "ms": 5, "result": "Monitor 1"},
            {"tool": "app", "args": {"action": "click", "target": "Instagram", "app": "firefox"}, "ok": True, "ms": 200,
             "result": "Clicked Instagram in Firefox."}]

    def available(self):
        return True

    async def run(self, task, context="", on_step=None, long=False):
        self.calls.append((task, context))
        self.long = long
        for s in self.steps:
            on_step(s)
            await asyncio.sleep(0)
        return A.AgentResult(self.ok, self.say, list(self.steps))


def with_agent(brain, fake):
    brain._agent_obj = fake
    return fake


async def test_owners_case_press_the_instagram_is_worked_out_then_learned(local_settings, ctx, monkeypatch):
    """'Press the Instagram' -> 'I cannot press Instagram' (owner's screenshot). Now a failed click
    goes to Claude, the steps show live, and the next time is instant with no Claude."""
    brain, fake = make(local_settings, [])
    agent = with_agent(brain, FakeAgent())

    def click_fails(args, c):
        from assistant.tools.registry import ToolError
        raise ToolError("I can't see 'instagram' in Firefox.")
    brain.registry._tools["click_element"].handler = click_fails
    brain.registry._tools["app"].handler = lambda a, c: "Clicked Instagram in Firefox."
    conv = Conversation()
    events = await collect(brain, conv, "Go ahead and press the Instagram.", ctx)
    text = said(events)
    assert text.startswith("Let me work that out, sir.") and "can't see" not in text
    assert "Opened Instagram." in text and "learned how" in text
    names = [e.name for e in events if isinstance(e, ToolStarted)]
    assert names == ["click_element", "work_it_out", "screenshot", "app"]              # live in the feed
    assert agent.calls[0][0] == "Go ahead and press the Instagram." and "can't see" in agent.calls[0][1]
    # Next time: the learned steps, no Claude.
    agent.calls.clear()
    events = await collect(brain, Conversation(), "Go ahead and press the Instagram.", ctx)
    assert agent.calls == [] and said(events) == "Clicked Instagram in Firefox."


async def test_model_saying_it_cannot_goes_to_claude(local_settings, ctx):
    brain, fake = make(local_settings, [text_reply("I can't do that, sir."), text_reply("I cannot do that.")])
    agent = with_agent(brain, FakeAgent(steps=[]))
    events = await collect(brain, Conversation(), "rearrange my discord servers alphabetically", ctx)
    assert "wasn't able" not in said(events) and said(events).startswith("Let me work that out")
    assert agent.calls


async def test_failure_from_claude_is_said_plainly(local_settings, ctx):
    brain, fake = make(local_settings, [])
    with_agent(brain, FakeAgent(ok=False, say="The site needs you to log in first", steps=[]))
    brain.registry._tools["click_element"].handler = lambda a, c: (_ for _ in ()).throw(__import__(
        "assistant.tools.registry", fromlist=["ToolError"]).ToolError("I can't see 'login'."))
    events = await collect(brain, Conversation(), "click login", ctx)
    assert said(events).endswith("The site needs you to log in first.") and L.get_lessons().items() == []


async def test_deliberate_refusals_are_not_worked_around(local_settings, ctx):
    """A banned site or a declined confirmation must never be 'worked out' by Claude."""
    from assistant.tools import sitecheck
    sitecheck.block("sketchy.example")
    brain, fake = make(local_settings, [])
    agent = with_agent(brain, FakeAgent())
    events = await collect(brain, Conversation(), "open sketchy.example", ctx)
    assert "never to open" in said(events) and agent.calls == []


async def test_from_the_phone_only_after_a_yes_on_the_phone(local_settings):
    brain, fake = make(local_settings, [text_reply("I can't do that from here."), text_reply("Fine.")])
    agent = with_agent(brain, FakeAgent(steps=[]))
    asked = []

    async def no(tool, args):
        asked.append(brain.registry.describe(tool, args))
        return False
    events = await collect(brain, Conversation(), "figure it out", ToolContext(local_settings, remote=True, confirm=no))
    assert agent.calls == [] and asked and asked[0].startswith("let Claude control the PC")
    assert said(events) == "All right, sir, I'll leave it."

    async def yes(tool, args):
        return True
    await collect(brain, Conversation(), "figure it out", ToolContext(local_settings, remote=True, confirm=yes))
    assert agent.calls
    local_settings.phone.pc_control = "off"
    agent.calls.clear()
    await collect(brain, Conversation(), "figure it out", ToolContext(local_settings, remote=True, confirm=yes))
    assert agent.calls == []


async def test_figure_it_out_uses_the_last_request(local_settings, ctx):
    brain, fake = make(local_settings, [text_reply("Here's a joke, sir.")])
    agent = with_agent(brain, FakeAgent(steps=[]))
    conv = Conversation()
    await collect(brain, conv, "sort my desktop icons by name", ctx)
    events = await collect(brain, conv, "Nova, figure it out", ctx)
    assert agent.calls[0][0] == "sort my desktop icons by name" and said(events).startswith("Let me work that out")


async def test_a_working_model_turn_is_left_alone(local_settings, ctx):
    brain, fake = make(local_settings, [tool_reply("volume", {"action": "up"}), text_reply("Louder, sir.")])
    agent = with_agent(brain, FakeAgent())
    brain.registry._tools["volume"].handler = lambda a, c: "Volume 50."
    events = await collect(brain, Conversation(), "bump the sound a little would you", ctx)
    assert agent.calls == [] and not any(isinstance(e, ToolFinished) and e.name == "work_it_out" for e in events)


async def test_questions_nova_cant_answer_stay_answers(local_settings, ctx):
    """'I can't predict that' to a question is an answer, not something to hand over."""
    brain, fake = make(local_settings, [text_reply("I can't predict the lottery, sir.")])
    agent = with_agent(brain, FakeAgent())
    events = await collect(brain, Conversation(), "what numbers will win the lottery", ctx)
    assert said(events) == "I can't predict the lottery, sir." and agent.calls == []


async def test_normal_replies_are_not_delayed_or_changed(local_settings, ctx):
    brain, fake = make(local_settings, [text_reply("Paris is the capital of France. It's lovely in spring.")])
    with_agent(brain, FakeAgent())
    events = await collect(brain, Conversation(), "tell me about paris", ctx)
    assert said(events) == "Paris is the capital of France. It's lovely in spring."


@pytest.mark.parametrize("reply", [
    "Certainly, sir. Unfortunately, that's not something I'm able to do.",
    "I'm afraid that's beyond my capabilities, sir.",
    "Of course. I don't have permission to change that setting.",
])
async def test_told_to_do_something_it_never_just_says_it_cant(local_settings, ctx, reply):
    """Owner: "if I tell it to do something then it's not gonna tell me oh sir I can't do that,
    if I tell it to do something it does it". Every way of saying no (even in a later sentence)
    goes to Claude, who works it out on the PC."""
    brain, fake = make(local_settings, [text_reply(reply), text_reply(reply)])
    agent = with_agent(brain, FakeAgent(steps=[]))
    events = await collect(brain, Conversation(), "turn on night light in windows settings", ctx)
    text = said(events)
    assert agent.calls and text.startswith("Let me work that out") and "not something" not in text



# --- "complete the task on my screen" ---------------------------------------------------------------
@pytest.mark.parametrize("phrase", ["hey nova go ahead and complete this assignment for me right",
                                    "Nova, do my homework", "answer these questions",
                                    "complete the task on my screen right", "Nova, fill out this survey for me",
                                  "finish the form on my screen", "do what's on my screen"])
async def test_a_whole_task_on_the_screen_is_seen_through(local_settings, ctx, phrase):
    """Owner: "say complete the task on my screen and he will go through and complete the task until
    it's finished, like a real capable JARVIS". Straight to Claude, in long mode; not a shortcut."""
    brain, fake = make(local_settings, [])
    agent = with_agent(brain, FakeAgent(say="Survey submitted, the thank-you page is showing", steps=[
        {"tool": "click_element", "args": {"name": "Very satisfied"}, "ok": True, "ms": 300, "result": "Clicked"},
        {"tool": "click_element", "args": {"name": "Submit"}, "ok": True, "ms": 300, "result": "Clicked"}]))
    events = await collect(brain, Conversation(), phrase, ctx)
    text = said(events)
    assert text.startswith("On it, sir.") and "thank-you page is showing" in text and agent.long is True
    assert not fake.requests and L.get_lessons().items() == []


def test_long_mode_gets_time_steps_and_the_task_instructions(settings, tmp_path):
    fake = type("E", (), {"executable": lambda self: "claude", "env": lambda self: {}})()
    agent = A.Agent(settings, expert=fake)
    quick, whole = agent.command(tmp_path / "m.json"), agent.command(tmp_path / "m.json", long=True)
    turns = lambda c: int(c[c.index("--max-turns") + 1])
    prompt = lambda c: c[c.index("--append-system-prompt") + 1]
    assert turns(whole) == settings.brain.agent.task_max_turns > turns(quick)
    # Owner: "switch to the homework tab, read all of the requirements and either do the quiz or ... open a
    # notepad and answer all the questions ... he should be able to figure out that he has to do that".
    assert "Read ALL of it" in prompt(whole) and "Read ALL of it" not in prompt(quick)
    assert "list_tabs" in prompt(whole) and "Notepad" in prompt(whole) and "numbered" in prompt(whole)
    assert "never buy or pay" in prompt(whole) and "passwords" in prompt(whole)


async def test_claude_can_scroll_and_wait(settings, registry, monkeypatch):
    from assistant.tools import pc
    monkeypatch.setattr(pc, "WINDOWS", type("W", (), {"active": lambda s: pc.Win(3, "Survey - Firefox", "firefox.exe"),
                                                       "rect": lambda s, h: (0, 0, 1000, 800),
                                                       "list": lambda s: [], "foreground": lambda s: 3})())
    tools = TS.NovaTools(settings, registry)
    names = {t["name"] for t in tools.list()}
    assert {"scroll", "wait", "look_at_screen"} <= names
    moves, wheel = [], []
    tools.ctx.services["grid"].mouse = type("M", (), {"move": lambda s, x, y: moves.append((x, y)),
                                                      "scroll": lambda s, n: wheel.append(n)})()
    content, ok = await tools.call("scroll", {"direction": "down", "amount": 6})
    assert ok and moves == [(500, 400)] and wheel == [-6]
    content, ok = await tools.call("wait", {"seconds": 0.5})
    assert ok and "Waited" in content[0]["text"]


@pytest.mark.parametrize("phrase", ["do the dishes", "answer the phone", "finish the song",
                                    "what is this assignment about"])
def test_everyday_requests_are_not_whole_tasks(phrase):
    from assistant.brain.local import _SCREEN_TASK
    assert not _SCREEN_TASK.match(phrase)
