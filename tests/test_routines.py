"""Routines: one phrase, several tools, same safety rules as the tools on their own."""

import pytest

from assistant.brain.llm import TextDelta
from assistant.core.config import DEFAULT_CONFIG_PATH, RoutineConfig, load_settings
from assistant.core.conversation import Conversation
from assistant.tools import build_registry, routines
from assistant.tools.registry import AuditLog, Risk, ToolContext
from tests.test_local_brain import collect, make


def add_fakes(reg, log):
    def fake(name, risk=Risk.SAFE, fail=None):
        async def run(args, ctx):
            log.append((name, args))
            if fail:
                from assistant.tools.registry import ToolError
                raise ToolError(fail)
            return f"{name} ok"
        reg.get(name).handler = run
        if risk != reg.get(name).risk:
            reg.get(name).risk = risk
    return fake


@pytest.fixture
def game(settings):
    settings.routines = {
        "gaming_mode": RoutineConfig.model_validate({
            "phrases": ["gaming mode", "time to game"], "reply": "Gaming mode, sir.",
            "steps": [{"tool": "open_app", "args": {"name": "steam"}},
                      {"wait": 0},
                      {"tool": "music_control", "args": {"action": "pause"}, "optional": True},
                      {"tool": "volume", "args": {"action": "set", "level": 60}}]}),
        "goodnight": RoutineConfig.model_validate({
            "phrases": ["goodnight"], "steps": [{"tool": "volume", "args": {"action": "set", "level": 20}},
                                                {"tool": "power", "args": {"action": "sleep"}}]}),
        "off_one": None,
    }
    return settings


def test_matching_is_exact_after_filler(game):
    for said in ["Gaming mode.", "start gaming mode", "Turn on gaming mode please", "time to game!",
                 "switch to the gaming mode routine", "gaming mode"]:
        assert routines.match_routine(said, game) == ("run_routine", {"name": "gaming_mode"}), said
    for said in ["what is gaming mode", "gaming mode off", "tell me about goodnight moon", "off one", ""]:
        assert routines.match_routine(said, game) is None, said


async def test_runs_steps_in_order(game):
    reg = build_registry(game, AuditLog(game.safety.audit_path()))
    log = []
    fake = add_fakes(reg, log)
    fake("open_app")
    fake("music_control", fail="Spotify isn't linked")        # optional: not mentioned
    fake("volume")
    res = await reg.execute("run_routine", {"name": "gaming_mode"}, ToolContext(game))
    assert res.content == "Gaming mode, sir."
    assert [n for n, _ in log] == ["open_app", "music_control", "volume"]
    audit = [r["tool"] for r in reg.audit.tail(10)]
    assert audit == ["open_app", "music_control", "volume", "run_routine"]   # each step audited


async def test_risky_steps_still_ask(game):
    game.phone.pc_control = "off"          # the "phone can't control the PC" setting
    reg = build_registry(game, AuditLog(game.safety.audit_path()))
    log, asked = [], []
    fake = add_fakes(reg, log)
    fake("volume")
    fake("power", risk=Risk.CONFIRM)

    async def no(tool, args):
        asked.append(reg.describe(tool, args))
        return False
    res = await reg.execute("run_routine", {"name": "goodnight"}, ToolContext(game, confirm=no))
    assert asked == ["put the PC to sleep"] and [n for n, _ in log] == ["volume"]
    assert res.content == "Goodnight is done. Except: put the PC to sleep (you said no)."
    remote = await reg.execute("run_routine", {"name": "goodnight"},
                               ToolContext(game, remote=True, confirm=no))
    assert "blocked" in remote.content and len(asked) == 1          # never even asked from a phone


async def test_failures_are_reported(game):
    reg = build_registry(game, AuditLog(game.safety.audit_path()))
    fake = add_fakes(reg, [])
    fake("open_app", fail="I couldn't find Steam")
    fake("volume", fail="No audio device")
    res = await reg.execute("run_routine", {"name": "gaming_mode"}, ToolContext(game))
    assert res.content.startswith("None of gaming mode worked: ") and "couldn't find Steam" in res.content


def test_problems_found(settings, registry):
    settings.routines = {"bad": RoutineConfig.model_validate(
        {"steps": [{"tool": "make_coffee"}, {"tool": "run_routine", "args": {"name": "bad"}}]}),
        "empty": RoutineConfig()}
    found = routines.problems(settings, registry)
    assert any("make_coffee" in p for p in found) and any("another routine" in p for p in found)
    assert any("empty: has no steps" in p for p in found)


def test_builtin_routines_are_valid():
    s = load_settings(DEFAULT_CONFIG_PATH)
    reg = build_registry(s)
    assert {"gaming_mode", "goodnight"} <= set(routines.active(s))
    assert routines.problems(s, reg) == []
    assert "gaming mode" in reg.get("run_routine").description


async def test_unknown_routine(settings, registry):
    res = await registry.execute("run_routine", {"name": "nope"}, ToolContext(settings))
    assert res.is_error and "no routine called" in res.content


async def test_voice_phrase_skips_the_model(game):
    game.brain.backend = "local"
    brain, fake = make(game, [])
    log = []
    f = add_fakes(brain.registry, log)
    for name in ("open_app", "music_control", "volume"):
        f(name)
    events = await collect(brain, Conversation(), "Start gaming mode", ToolContext(game))
    assert fake.requests == [] and len(log) == 3
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Gaming mode, sir."
