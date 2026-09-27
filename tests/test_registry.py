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
