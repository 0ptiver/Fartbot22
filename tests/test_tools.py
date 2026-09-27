import base64
import io

from PIL import Image

from assistant.tools import screen, system
from assistant.tools.registry import ToolError


def test_get_time(ctx):
    out = system.get_time({}, ctx)
    assert "America/Chicago" in out


class FakeEndpoint:
    def __init__(self, level=0.5, muted=0):
        self.level, self.muted = level, muted

    def GetMasterVolumeLevelScalar(self):
        return self.level

    def SetMasterVolumeLevelScalar(self, v, _):
        self.level = v

    def GetMute(self):
        return self.muted

    def SetMute(self, m, _):
        self.muted = m


def test_volume_actions(ctx):
    ep = FakeEndpoint(0.5)
    assert system.volume({"action": "get"}, ctx, ep) == "Volume is 50%"
    system.volume({"action": "up", "amount": 20}, ctx, ep)
    assert round(ep.level * 100) == 70
    system.volume({"action": "down", "amount": 90}, ctx, ep)
    assert ep.level == 0
    system.volume({"action": "mute"}, ctx, ep)
    assert ep.muted == 1
    system.volume({"action": "set", "level": 30}, ctx, ep)
    assert round(ep.level * 100) == 30 and ep.muted == 0  # setting a level unmutes


def test_volume_set_requires_level(ctx):
    try:
        system.volume({"action": "set"}, ctx, FakeEndpoint())
    except ToolError:
        return
    raise AssertionError


def test_open_app_alias(ctx):
    launched = []
    out = system.open_app({"name": "Spotify"}, ctx, _launcher=launched.append, _dirs=[])
    assert launched == ["spotify:"] and "Spotify" in out


def test_open_app_start_menu(ctx, tmp_path):
    progs = tmp_path / "Programs"
    (progs / "Steam").mkdir(parents=True)
    (progs / "Steam" / "Steam.lnk").write_text("")
    (progs / "Steam" / "Uninstall Steam.lnk").write_text("")
    (progs / "Steam Tools Helper.lnk").write_text("")
    launched = []
    out = system.open_app({"name": "steam"}, ctx, _launcher=launched.append, _dirs=[progs])
    assert launched == [str(progs / "Steam" / "Steam.lnk")] and out == "Opened Steam."


def test_open_app_missing(ctx, tmp_path):
    try:
        system.open_app({"name": "nothing"}, ctx, _launcher=lambda t: None, _dirs=[tmp_path])
    except ToolError as e:
        assert "couldn't find" in str(e)
        return
    raise AssertionError


def test_look_at_screen_resizes(ctx):
    big = Image.new("RGB", (3840, 2160), "blue")
    res = screen.look_at_screen({"focus": "error"}, ctx, _grab=lambda m: big)
    img_block, text_block = res.content
    assert img_block["type"] == "image" and "error" in text_block["text"]
    img = Image.open(io.BytesIO(base64.b64decode(img_block["source"]["data"])))
    assert max(img.size) == 1568


async def test_registry_passes_schema(registry, ctx):
    res = await registry.execute("volume", {"action": "explode"}, ctx)
    assert res.is_error and "Invalid arguments" in res.content
