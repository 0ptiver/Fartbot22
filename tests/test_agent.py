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
from tests.test_local_brain import FakeExpert, collect, make, text_reply, tool_reply


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



# --- whole jobs, in the background -----------------------------------------------------------------
class FakeVoice:
    def __init__(self):
        self.said, self.events = [], []

    async def announce(self, text):
        self.said.append(text)

    def on_event(self, ev):
        self.events.append(ev)


async def started(brain):
    for _ in range(200):                                # bounded: never hangs the tests
        if brain.jobs.current.note:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the job never started")


class SlowAgent(FakeAgent):
    """Works until told to finish, reporting progress like Claude would."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.go = asyncio.Event()

    async def run(self, task, context="", on_step=None, long=False):
        self.calls.append((task, context))
        self.long = long
        on_step({"tool": "look_at_screen", "args": {}, "ok": True, "ms": 100, "result": "Assignment: 5 questions"})
        on_step({"tool": "progress", "args": {"note": "answered 3 of 5 questions"}, "ok": True, "ms": 0})
        await self.go.wait()
        on_step({"tool": "write_document", "args": {"title": "Answers"}, "ok": True, "ms": 50, "result": "Saved"})
        return A.AgentResult(self.ok, self.say, [])


@pytest.mark.parametrize("phrase", ["hey nova go ahead and complete this assignment for me right",
                                    "Nova, do my homework", "answer these questions",
                                    "complete the task on my screen right",
                                    "Nova, fill out this survey for me", "do what's on my screen"])
async def test_a_whole_job_starts_in_the_background(local_settings, ctx, phrase):
    brain, fake = make(local_settings, [])
    agent = with_agent(brain, SlowAgent(say="All five answers are saved in Documents"))
    events = await collect(brain, Conversation(), phrase, ctx)
    assert said(events).startswith("On it, sir.") and brain.jobs.running() and not fake.requests
    agent.go.set()
    await brain.jobs.current.handle
    assert agent.long is True and brain.jobs.current.status == "done" and L.get_lessons().items() == []


async def test_nova_keeps_working_while_answering_how_far(local_settings, ctx):
    """Owner's case: "What are you doing right now?" cancelled the job, then Nova made up "I am still
    working on your assignment". Now the job carries on, and he answers from its real progress."""
    brain, fake = make(local_settings, [])
    agent = with_agent(brain, SlowAgent(say="All five answers are saved in Documents"))
    voice = ctx.services["voice"] = FakeVoice()
    conv = Conversation()
    await collect(brain, conv, "go ahead and complete this assignment for me", ctx)
    await started(brain)
    for question in ("What are you doing right now?", "are you still working?", "how far have you gotten?"):
        reply = said(await collect(brain, conv, question, ctx))
        assert reply.startswith("Still on it, sir: answered 3 of 5 questions."), reply
    assert brain.jobs.running() and not fake.requests                  # never cancelled, no model guess
    assert any(e.get("name") == "look_at_screen" for e in voice.events)  # steps still reach the live feed
    agent.go.set()
    await brain.jobs.current.handle
    assert voice.said == ["All done, sir. All five answers are saved in Documents."]
    assert said(await collect(brain, conv, "is it done?", ctx)).startswith("All done, sir.")
    assert "background job" in conv.messages[-3]["content"]


async def test_stop_halts_the_job(local_settings, ctx):
    brain, fake = make(local_settings, [])
    with_agent(brain, SlowAgent())
    voice = ctx.services["voice"] = FakeVoice()
    conv = Conversation()
    await collect(brain, conv, "do my homework", ctx)
    await started(brain)
    reply = said(await collect(brain, conv, "stop", ctx))
    assert reply == "Stopped, sir. I'd got as far as: answered 3 of 5 questions."
    await asyncio.sleep(0)
    assert brain.jobs.running() is None and brain.jobs.current.status == "stopped" and voice.said == []


async def test_one_job_at_a_time(local_settings, ctx):
    brain, fake = make(local_settings, [])
    agent = with_agent(brain, SlowAgent())
    conv = Conversation()
    await collect(brain, conv, "do my homework", ctx)
    reply = said(await collect(brain, conv, "fill out this survey for me", ctx))
    assert "still on the last one" in reply and len(agent.calls) <= 1
    agent.go.set()
    await brain.jobs.current.handle


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


def test_written_work_is_saved_as_a_file_never_typed(tmp_path, monkeypatch, settings, registry):
    """Owner's case: typing a long answer into Notepad came out jumbled. Claude saves written work
    with write_document: a new .txt in Documents\\Nova, opened in Notepad, never overwriting."""
    from assistant.core import paths
    monkeypatch.setattr(paths, "documents_folder", lambda: tmp_path)
    opened = []
    first = TS.write_document({"title": "Biomechanics answers", "text": "1. Levers\n2. Fulcrums"}, opened.append)
    again = TS.write_document({"title": "Biomechanics answers", "text": "other"}, opened.append)
    sneaky = TS.write_document({"title": "..\\..\\Windows\\evil", "text": "x"}, opened.append)
    folder = tmp_path / "Nova"
    assert (folder / "Biomechanics answers.txt").read_bytes() == b"1. Levers\r\n2. Fulcrums"
    assert (folder / "Biomechanics answers (2).txt").read_text() == "other"
    assert [p.parent for p in opened] == [folder] * 3 and "Notepad" in first and "(2)" in again
    assert opened[2].name == "Windowsevil.txt" and opened[2].parent == folder and "Saved" in sneaky
    assert "write_document" in {t["name"] for t in TS.NovaTools(settings, registry).list()}


async def test_follow_ups_to_a_finished_job_go_back_to_claude_with_what_it_wrote(local_settings, ctx):
    """Owner's case: after the assignment, "rewrite them like a high schooler", "shorten the answers"
    and "paste the shortened ones" went to the small model, which promised ("I'll rewrite them") and
    claimed ("Already pasted, sir") without doing anything."""
    brain, fake = make(local_settings, [])
    agent = with_agent(brain, SlowAgent(say="Five answers saved"))
    conv = Conversation()
    await collect(brain, conv, "complete the assignment on my screen", ctx)
    await started(brain)
    agent.go.set()
    await brain.jobs.current.handle
    brain.jobs.current.document = {"title": "3.07 Answers", "text": "1. Levers multiply force."}
    first = brain.jobs.current
    agent.go = asyncio.Event()
    reply = said(await collect(brain, conv, "Can you go ahead and shorten the answers?", ctx))
    assert reply.startswith("On it, sir.") and brain.jobs.current is not first and not fake.requests
    await started(brain)
    task, context = agent.calls[-1]
    assert task == "Can you go ahead and shorten the answers?"
    assert "1. Levers multiply force." in context and "write_document" in context
    agent.go.set()
    await brain.jobs.current.handle


async def test_turn_it_up_after_a_job_is_still_the_volume(local_settings, ctx):
    brain, fake = make(local_settings, [])
    agent = with_agent(brain, SlowAgent())
    await collect(brain, Conversation(), "do my homework", ctx)
    await started(brain)
    agent.go.set()
    await brain.jobs.current.handle
    calls = len(agent.calls)
    events = await collect(brain, Conversation(), "turn it up", ctx)
    assert len(agent.calls) == calls and any(isinstance(e, ToolStarted) and e.name == "volume" for e in events)


async def test_an_instruction_added_mid_job_reaches_it(local_settings, ctx):
    """Owner's case: an instruction given while a job ran was lost (the job was cancelled at question 2
    and the small model claimed it was all done). Now the job restarts with it, from where it got to."""
    brain, fake = make(local_settings, [])
    agent = with_agent(brain, SlowAgent(say="All saved"))
    voice = ctx.services["voice"] = FakeVoice()
    conv = Conversation()
    await collect(brain, conv, "complete the assignment on my screen", ctx)
    await started(brain)
    first = brain.jobs.current
    reply = said(await collect(brain, conv, "also save it as a Word file", ctx))
    assert reply == "Got it, sir. I'll carry on with that." and first.status == "stopped"
    for _ in range(200):                                  # bounded wait for the fresh session
        if len(agent.calls) == 2:
            break
        await asyncio.sleep(0.005)
    task, context = agent.calls[-1]
    assert task == "complete the assignment on my screen"
    assert "answered 3 of 5 questions" in context and "also save it as a Word file" in context
    assert "don't redo finished parts" in context
    agent.go.set()
    await brain.jobs.current.handle
    assert voice.said == ["All done, sir. All saved."] and not fake.requests


def test_longer_ways_of_asking_are_still_whole_jobs():
    from assistant.brain.local import _SCREEN_TASK
    assert _SCREEN_TASK.match("Go ahead and complete the complete like five questions of this quiz on my screen")
    assert _SCREEN_TASK.match("answer the questions in this worksheet")


async def test_pc_jobs_never_end_at_the_claude_that_cant_see_the_screen(local_settings, ctx):
    """Owner's case: "open a new tab and open a paper trading" -> the small model asked the
    question-answering Claude, which said "I can't see your screen or click in Firefox from here".
    Claude with Nova's hands takes it on instead."""
    expert = FakeExpert("I can't see your screen or click in Firefox from here, so nothing was filled in.")
    brain, fake = make(local_settings, [tool_reply("escalate", {"task": "open a new tab and paper trading"}),
                                        text_reply("I can't see your screen, sir.")], expert)
    agent = with_agent(brain, FakeAgent(say="Opened a new tab with a paper trading site", steps=[]))
    events = await collect(brain, Conversation(), "open a new tab and open a paper trading", ctx)
    text = said(events)
    assert agent.calls and "can't see your screen" not in text and "paper trading site" in text


async def test_a_refused_pc_action_goes_to_claude_with_hands(local_settings, ctx):
    brain, fake = make(local_settings, [text_reply("I'm sorry, but I can't help with that.")])
    agent = with_agent(brain, FakeAgent(say="Done", steps=[]))
    events = await collect(brain, Conversation(), "open a new tab and open a paper trading", ctx)
    assert agent.calls and said(events).startswith("Let me work that out")


@pytest.mark.parametrize("phrase,task", [
    ("work it out and open a new tab with paper trading", "open a new tab with paper trading"),
    ("use claude to open a new tab", "open a new tab"),
    ("Claude, open a new tab", "open a new tab"),
    ("get claude to open paper trading", "open paper trading"),
    ("figure out how to open paper trading", "open paper trading"),
    ("ask claude to open a new tab", "open a new tab"),
])
async def test_asking_for_claude_gets_claude_with_hands_on_what_was_said(local_settings, ctx, phrase, task):
    """Owner: "I'm saying things like work it out but it keeps just using Nova instead of contacting
    Claude". "Work it out and X" used the previous request; "use Claude to X" / "Claude, X" weren't
    caught; "ask Claude to open X" went to the Claude that can't see the screen."""
    brain, fake = make(local_settings, [])
    agent = with_agent(brain, FakeAgent(say="Done", steps=[]))
    events = await collect(brain, Conversation(), phrase, ctx)
    assert agent.calls and agent.calls[0][0] == task and not fake.requests
    assert said(events).startswith("Let me work that out")


async def test_questions_for_claude_still_go_to_the_answering_claude(local_settings, ctx):
    expert = FakeExpert("Because of Rayleigh scattering.")
    brain, fake = make(local_settings, [text_reply("It's Rayleigh scattering, sir.")], expert)
    agent = with_agent(brain, FakeAgent(steps=[]))
    await collect(brain, Conversation(), "ask claude why the sky is blue", ctx)
    assert expert.calls and not agent.calls


async def test_says_why_when_claude_cant_take_it(local_settings, ctx):
    """Never quietly carry on as Nova when Claude was asked for."""
    brain, fake = make(local_settings, [])
    local_settings.brain.agent.enabled = False
    events = await collect(brain, Conversation(), "use claude to open a new tab", ctx)
    assert "I can't hand that to Claude right now, sir: handing jobs to Claude is switched off" in said(events)
    assert not fake.requests


# --- long jobs that use up a run's steps ----------------------------------------------------------
MAX_TURNS_JSON = (b'{"type":"result","subtype":"error_max_turns","is_error":true,"num_turns":251,'
                  b'"errors":["Reached maximum number of turns (250)"],"terminal_reason":"max_turns"}')


def test_running_out_of_turns_is_named_not_exit_1():
    """Owner's case: long jobs died after 5-10 minutes with "Claude Code failed: exit 1". That's how
    Claude Code (checked with the real CLI) reports running out of turns: exit 1, nothing on stderr,
    the reason only in "errors"."""
    from assistant.brain.expert import ClaudeCodeExpert, ExpertError, OutOfSteps
    with pytest.raises(OutOfSteps, match="ran out of steps"):
        ClaudeCodeExpert._parse(1, MAX_TURNS_JSON, b"")
    with pytest.raises(ExpertError, match="Something odd"):
        ClaudeCodeExpert._parse(1, b'{"is_error":true,"subtype":"error_during_execution","errors":["Something odd"]}', b"")


class StepHungryAgent(FakeAgent):
    """Uses up its steps a few times before finishing, like a long job does."""

    def __init__(self, runs_needed, **kw):
        super().__init__(**kw)
        self.runs_needed = runs_needed

    async def run(self, task, context="", on_step=None, long=False):
        from assistant.brain.expert import OutOfSteps
        self.calls.append((task, context))
        on_step({"tool": "look_at_screen", "args": {}, "ok": True, "ms": 100, "result": "Page"})
        on_step({"tool": "progress", "args": {"note": f"part {len(self.calls)} done"}, "ok": True, "ms": 0})
        if len(self.calls) < self.runs_needed:
            raise OutOfSteps("Claude ran out of steps")
        return A.AgentResult(True, self.say, [])


async def test_a_long_job_carries_on_in_a_fresh_run(local_settings, ctx):
    brain, fake = make(local_settings, [])
    agent = with_agent(brain, StepHungryAgent(3, say="Finished every part"))
    voice = ctx.services["voice"] = FakeVoice()
    await collect(brain, Conversation(), "do my homework", ctx)
    await brain.jobs.current.handle
    assert len(agent.calls) == 3 and brain.jobs.current.status == "done"
    assert "ran out of steps" in agent.calls[1][1] and "part 1 done" in agent.calls[1][1]
    assert "part 2 done" in agent.calls[2][1] and brain.jobs.current.steps == 3
    assert voice.said == ["All done, sir. Finished every part."]


async def test_a_job_that_never_finishes_stops_after_its_runs(local_settings, ctx):
    local_settings.brain.agent.task_runs = 2
    brain, fake = make(local_settings, [])
    agent = with_agent(brain, StepHungryAgent(99))
    voice = ctx.services["voice"] = FakeVoice()
    await collect(brain, Conversation(), "do my homework", ctx)
    await brain.jobs.current.handle
    assert len(agent.calls) == 2 and brain.jobs.current.status == "failed"
    assert voice.said == ["I had to stop, sir: Claude ran out of steps after 2 runs and 2 steps."]
    assert "exit" not in voice.said[0]
