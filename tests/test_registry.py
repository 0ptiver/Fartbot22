import asyncio
import threading

from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry


def make(settings):
    reg = ToolRegistry(settings)
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}

    @reg.tool("double", "double n", schema)
    def double(args, ctx):
        return {"thread": threading.current_thread().name, "value": args["n"] * 2}

    @reg.tool("danger", "needs confirm", schema, risk=Risk.CONFIRM)
    async def danger(args, ctx):
        return "did it"

    @reg.tool("boom", "raises", {"type": "object"})
    def boom(args, ctx):
        raise ToolError("nope")

    @reg.tool("crash", "raises unexpectedly", {"type": "object"})
    def crash(args, ctx):
        raise ZeroDivisionError("x")

    return reg


async def test_sync_handler_runs_off_loop_and_is_audited(settings):
    reg = make(settings)
    res = await reg.execute("double", {"n": 21}, ToolContext(settings))
    assert not res.is_error
    assert '"value": 42' in res.content
    assert "MainThread" not in res.content
    log = reg.audit.tail()
    assert log[-1]["tool"] == "double" and log[-1]["status"] == "ok"


async def test_schema_validation(settings):
    reg = make(settings)
    res = await reg.execute("double", {"n": "x"}, ToolContext(settings))
    assert res.is_error and "Invalid arguments" in res.content
    res = await reg.execute("double", "not a dict", ToolContext(settings))
    assert res.is_error and "INVALID_JSON" in res.content


async def test_unknown_tool(settings):
    res = await make(settings).execute("nope", {}, ToolContext(settings))
    assert res.is_error


async def test_confirm_declined_without_callback(settings):
    reg = make(settings)
    res = await reg.execute("danger", {"n": 1}, ToolContext(settings))
    assert res.is_error and "declined" in res.content
    assert reg.audit.tail()[-1]["status"] == "declined"


async def test_confirm_approved(settings):
    reg = make(settings)
    seen = []

    async def yes(name, args):
        seen.append((name, args))
        return True

    res = await reg.execute("danger", {"n": 1}, ToolContext(settings, confirm=yes))
    assert res.content == "did it" and seen == [("danger", {"n": 1})]


async def test_risk_override_and_remote_block(settings):
    settings.phone.pc_control = "off"          # the "phone can't control the PC" setting
    settings.safety.risk_overrides = {"double": "blocked"}
    settings.safety.remote_blocked_tools = ["danger"]
    reg = make(settings)
    assert (await reg.execute("double", {"n": 1}, ToolContext(settings))).is_error
    assert "double" not in [d["name"] for d in reg.definitions()]
    remote = ToolContext(settings, remote=True, confirm=lambda *_: asyncio.sleep(0, True))
    res = await reg.execute("danger", {"n": 1}, remote)
    assert res.is_error and "remote" in res.content


async def test_errors_do_not_raise(settings):
    reg = make(settings)
    assert (await reg.execute("boom", {}, ToolContext(settings))).content == "nope"
    res = await reg.execute("crash", {}, ToolContext(settings))
    assert res.is_error and "ZeroDivisionError" in res.content


def test_definitions_sorted_and_eager(settings):
    defs = make(settings).definitions()
    assert [d["name"] for d in defs] == sorted(d["name"] for d in defs)
    assert all(d["eager_input_streaming"] for d in defs)


def test_duplicate_rejected(settings):
    reg = make(settings)
    try:
        reg.tool("double", "again")(lambda a, c: None)
    except ValueError:
        return
    raise AssertionError("duplicate accepted")


def test_local_config_overrides(tmp_path):
    from assistant.core.config import load_settings
    base = tmp_path / "config.yaml"
    local = tmp_path / "local.yaml"
    base.write_text("voice:\n  mode: ptt\n  input_device: null\n")
    local.write_text("voice:\n  input_device: 3\n")
    s = load_settings(base, local)
    assert s.voice.input_device == 3 and s.voice.mode == "ptt"


def test_every_tool_schema_is_one_ollama_accepts(settings):
    """Owner's case: one tool schema without "properties" made Ollama reject the whole list
    ("properties must be an object") and Nova couldn't start."""
    from assistant.tools import build_registry
    settings.brain.backend = "local"
    reg = build_registry(settings)

    def check(schema, where):
        if schema.get("type") == "object" or schema.get("type") == ["object", "null"]:
            assert isinstance(schema.get("properties", {}), dict), where
        for key, sub in (schema.get("properties") or {}).items():
            assert isinstance(sub, dict), f"{where}.{key}"
            check(sub, f"{where}.{key}")
    for d in reg.definitions():
        assert d["input_schema"].get("type") == "object", d["name"]
        assert isinstance(d["input_schema"].get("properties"), dict), d["name"]
        check(d["input_schema"], d["name"])
    names = {d["name"] for d in reg.definitions()}
    assert "replay_click" not in names and "replay_keys" not in names       # internal: never offered


def test_schema_without_properties_is_fixed(settings):
    from assistant.tools.registry import ToolRegistry
    reg = ToolRegistry(settings)
    reg.tool("bare", "x", {"type": "object"})(lambda a, c: "ok")
    assert reg.get("bare").input_schema == {"type": "object", "properties": {}}


def test_the_prompt_only_names_tools_the_model_can_see(settings):
    """The prompt told the model to use mouse_grid, show_numbers and dictation, which are hidden
    from it (it can't know their arguments, so it guessed)."""
    import re as _re

    from assistant.brain.prompts import system_prompt
    from assistant.tools import build_registry
    reg = build_registry(settings)
    visible = {d["name"] for d in reg.definitions()}
    hidden = set(reg._tools) - visible
    named = set(_re.findall(r"\b([a-z]+(?:_[a-z]+)+|[a-z]+)\b", system_prompt(settings, local=True)))
    assert not (named & hidden - {"mouse", "subtitles", "teach", "lessons"}) , named & hidden
    assert "(mouse" not in system_prompt(settings, local=True)
