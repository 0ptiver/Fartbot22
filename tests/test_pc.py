"""PC control tools: status, lock/power (confirm), windows, websites. Windows APIs are faked."""

from types import SimpleNamespace

import pytest

from assistant.tools import pc
from assistant.tools.registry import ToolContext, ToolError


@pytest.fixture
def power(monkeypatch):
    calls = []
    monkeypatch.setattr(pc.POWER, "lock", lambda: calls.append("lock"))
    monkeypatch.setattr(pc.POWER, "sleep", lambda: calls.append("sleep"))
    monkeypatch.setattr(pc.POWER, "shutdown", lambda a: calls.append(a))
    return calls


async def yes(*a): return True
async def no(*a): return False


async def test_lock_needs_no_question(settings, registry, power):
    res = await registry.execute("lock_pc", {}, ToolContext(settings, confirm=no))
    assert res.content == "Locked." and power == ["lock"]


async def test_power_always_asks(settings, registry, power):
    res = await registry.execute("power", {"action": "shutdown"}, ToolContext(settings, confirm=no))
    assert res.is_error and power == []
    res = await registry.execute("power", {"action": "shutdown"}, ToolContext(settings, confirm=yes))
    assert "60 seconds" in res.content and power[0][:3] == ["/s", "/t", "60"]
    await registry.execute("power", {"action": "restart"}, ToolContext(settings, confirm=yes))
    assert power[1][0] == "/r"
    await registry.execute("power", {"action": "sleep"}, ToolContext(settings, confirm=yes))
    assert power[2] == "sleep"
    assert registry.describe("power", {"action": "sleep"}) == "put the PC to sleep"
    assert registry.describe("power", {"action": "shutdown"}) == "shut down the PC"


async def test_power_blocked_remotely(settings, registry, power):
    res = await registry.execute("power", {"action": "sleep"}, ToolContext(settings, remote=True, confirm=yes))
    assert res.is_error and power == []


async def test_cancel_shutdown(settings, registry, power):
    res = await registry.execute("cancel_shutdown", {}, ToolContext(settings))
    assert res.content == "Shutdown cancelled." and power == [["/a"]]


def test_real_backends_refuse_off_windows(monkeypatch, settings):
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    with pytest.raises(ToolError, match="Windows"):
        pc.lock_pc({}, ToolContext(settings))
    with pytest.raises(ToolError, match="Windows"):
        pc.window_control({"action": "list"}, ToolContext(settings))


class FakeWindows:
    def __init__(self):
        self.wins = [pc.Win(1, "Spotify Premium", "Spotify.exe"),
                     pc.Win(2, "Inbox - Gmail - Google Chrome", "chrome.exe"),
                     pc.Win(3, "Discord", "Discord.exe"),
                     pc.Win(4, "chrome tips.txt - Notepad", "notepad.exe")]
        self.calls = []

    def list(self):
        return self.wins

    def show(self, hwnd, how):
        self.calls.append((hwnd, how))

    def focus(self, hwnd):
        self.calls.append((hwnd, "focus"))
        return True

    def show_desktop(self):
        self.calls.append("desktop")


@pytest.fixture
def wins(monkeypatch):
    fake = FakeWindows()
    monkeypatch.setattr(pc, "WINDOWS", fake)
    return fake


def test_window_matching_prefers_process_name(settings, wins):
    ctx = ToolContext(settings)
    assert pc.window_control({"action": "focus", "app": "Chrome"}, ctx) == "Switched to chrome."
    assert wins.calls[-1] == (2, "focus")                    # not Notepad's "chrome tips.txt"
    assert pc.window_control({"action": "minimize", "app": "spotify"}, ctx) == "Minimized Spotify."
    assert wins.calls[-1] == (1, "minimize")
    pc.window_control({"action": "focus", "app": "gmail"}, ctx)   # title match
    assert wins.calls[-1] == (2, "focus")
    pc.window_control({"action": "show_desktop"}, ctx)
    assert wins.calls[-1] == "desktop"
    assert "Discord" in pc.window_control({"action": "list"}, ctx)
    with pytest.raises(ToolError, match="can't see"):
        pc.window_control({"action": "close", "app": "Photoshop"}, ctx)
    with pytest.raises(ToolError, match="Which app"):
        pc.window_control({"action": "close"}, ctx)


def test_open_website(settings):
    opened = []
    ctx = ToolContext(settings)
    assert pc.open_website({"site": "YouTube"}, ctx, _open=opened.append) == "Opened youtube.com."
    pc.open_website({"site": "bbc.co.uk"}, ctx, _open=opened.append)
    pc.open_website({"search": "lofi beats", "site": "youtube"}, ctx, _open=opened.append)
    out = pc.open_website({"search": "weather tomorrow"}, ctx, _open=opened.append)
    assert opened == ["https://www.youtube.com", "https://bbc.co.uk",
                      "https://www.youtube.com/results?search_query=lofi+beats",
                      "https://www.google.com/search?q=weather+tomorrow"]
    assert out == "Searching Google for weather tomorrow."
    for bad in ["file:///C:/Windows/System32/cmd.exe", "javascript://alert(1)", "ms-settings://x", ""]:
        with pytest.raises(ToolError, match="http"):
            pc.open_website({"site": bad}, ctx, _open=opened.append)
    assert len(opened) == 4


def test_system_status(settings, monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 12.0)
    monkeypatch.setattr(psutil, "sensors_battery",
                        lambda: SimpleNamespace(percent=81, power_plugged=False, secsleft=5400))
    gpu = lambda: {"name": "RTX 5070", "temp": 64, "util": 30, "mem_used": 2048, "mem_total": 8192}  # noqa: E731
    ctx = ToolContext(settings)
    out = pc.system_status({}, ctx, _gpu=gpu)
    assert out.startswith("CPU 12% busy") and "GPU 64°C" in out and "8 GB video memory" in out
    assert "battery 81% on battery, about 1h 30m left" in out
    assert pc.system_status({"what": "gpu"}, ctx, _gpu=lambda: None).startswith("I can't read the GPU")
    assert "free of" in pc.system_status({"what": "disk"}, ctx)
    assert pc.system_status({"what": "uptime"}, ctx).startswith("On for")


async def test_press_key_focuses_the_app_first(settings, registry, wins, monkeypatch):
    pressed = []
    wins.press = lambda vk: pressed.append(vk)
    res = await registry.execute("press_key", {"key": "f", "app": "chrome"}, ToolContext(settings))
    assert res.content == "Pressed f in chrome." and wins.calls[-1] == (2, "focus") and pressed == [0x46]
    bad = await registry.execute("press_key", {"key": "enter"}, ToolContext(settings))
    assert bad.is_error and pressed == [0x46]                  # only the allowlisted keys
    remote = await registry.execute("press_key", {"key": "space"}, ToolContext(settings, remote=True))
    assert remote.is_error and "blocked" in remote.content


def test_switching_that_windows_blocks_is_reported(settings, wins):
    wins.focus = lambda hwnd: False
    with pytest.raises(ToolError, match="wouldn't let me switch"):
        pc.window_control({"action": "focus", "app": "chrome"}, ToolContext(settings))
