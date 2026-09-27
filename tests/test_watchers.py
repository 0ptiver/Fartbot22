"""'Tell me when ...' watchers: each kind with fake probes, the manager, tools and phrases."""

import pytest

from assistant.brain.intents import match_intent
from assistant.core import watchers as W
from assistant.tools.registry import ToolContext


class FakeProbes(W.Probes):
    def __init__(self):
        self.partials, self.steam, self.procs, self.cpu = set(), set(), {}, 0.0
        self.gpu, self.cpu_total, self.batt, self.net, self.page = 70.0, 50.0, (50.0, True), False, "a"

    def partial_files(self):
        return set(self.partials)

    def steam_downloading(self):
        return set(self.steam)

    def processes(self, name):
        return self.procs.get(name.lower(), [])

    def cpu_of(self, procs):
        return self.cpu

    def gpu_temp(self):
        return self.gpu

    def cpu_percent(self):
        return self.cpu_total

    def battery(self):
        return self.batt

    def online(self):
        return self.net

    def page_fingerprint(self, url):
        return self.page


def test_download_finishes():
    p = FakeProbes()
    p.partials = {"C:/Users/me/Downloads/game-setup.exe.crdownload"}
    w = W.Watch("download")
    assert W.check(w, p) is None
    p.partials = set()
    assert W.check(w, p) == "Your download is finished: game-setup.exe."


def test_download_that_starts_later():
    p, w = FakeProbes(), W.Watch("download")
    assert W.check(w, p) is None                                   # nothing downloading yet
    p.partials = {"x/movie.mkv.part", "x/song.mp3.part"}
    assert W.check(w, p) is None
    p.partials = set()
    assert W.check(w, p) == "Your download is finished: 2 downloads."


def test_steam_app_and_system_watches():
    p = FakeProbes()
    p.steam = {"271590"}
    s = W.Watch("steam_download")
    assert W.check(s, p) is None
    p.steam = set()
    assert W.check(s, p) == "Steam has finished downloading."

    p.procs = {"blender": [object()]}
    done = W.Watch("app_done", "blender")
    p.cpu = 60.0
    assert W.check(done, p) is None                                # rendering
    p.cpu = 0.5
    assert [W.check(done, p) for _ in range(3)][-1] == "Blender looks finished: it's gone quiet."

    closes = W.Watch("app_closes", "blender")
    assert W.check(closes, p) is None
    p.procs = {}
    assert W.check(closes, p) == "Blender has closed."

    hot = W.Watch("gpu_above", value=85)
    assert W.check(hot, p) is None
    p.gpu = 88
    assert W.check(hot, p) == "Heads up: your GPU is at 88°C."
    cool = W.Watch("gpu_below")
    assert W.check(cool, p) is None
    p.gpu = 55
    assert W.check(cool, p) == "Your GPU has cooled down to 55°C."

    full = W.Watch("battery_above", value=100)
    p.batt = (99.4, True)
    assert W.check(full, p) == "The battery is full."
    low = W.Watch("battery_below", value=20)
    p.batt = (15, True)
    assert W.check(low, p) is None                                 # plugged in: no warning
    p.batt = (15, False)
    assert "15%" in W.check(low, p)

    net = W.Watch("internet_back")
    assert W.check(net, p) is None
    p.net = True
    assert W.check(net, p) == "The internet is back."

    page = W.Watch("page_changes", "https://www.bbc.co.uk/news")
    assert W.check(page, p) is None and W.check(page, p) is None
    p.page = "b"
    assert W.check(page, p) == "bbc.co.uk has changed."


async def test_manager_announces_once_and_limits():
    said = []
    p = FakeProbes()
    t = [1000.0]
    m = W.Watchers(notify=said.append, probes=p, clock=lambda: t[0])
    m.add(W.Watch("internet_back"))
    await m.tick()
    assert said == [] and len(m.items) == 1
    p.net = True
    t[0] += 10
    await m.tick()
    t[0] += 10
    await m.tick()
    assert said == ["The internet is back."] and m.items == {}
    with pytest.raises(W.WatchError, match="isn't running"):
        m.add(W.Watch("app_closes", "photoshop"))
    for _ in range(W.MAX_WATCHES):
        m.add(W.Watch("gpu_above", value=99))
    with pytest.raises(W.WatchError, match="already watching"):
        m.add(W.Watch("gpu_above", value=99))
    assert len(m.cancel("all")) == W.MAX_WATCHES


async def test_old_watches_expire():
    said = []
    t = [1000.0]
    m = W.Watchers(notify=said.append, probes=FakeProbes(), clock=lambda: t[0])
    w = m.add(W.Watch("gpu_above", value=99))
    w.created = t[0] - W.MAX_AGE_S - 1
    await m.tick()
    assert "it's been a day" in said[0] and m.items == {}


async def test_tools(settings, registry, monkeypatch):
    p = FakeProbes()
    ctx = ToolContext(settings, services={"watchers": W.Watchers(probes=p)})
    res = await registry.execute("watch", {"kind": "download"}, ctx)
    assert res.content == "I'll tell you when I notice your download finishing."
    from assistant.tools import uia
    monkeypatch.setattr(uia, "current_url", lambda: "https://www.youtube.com/watch?v=abc")
    res = await registry.execute("watch", {"kind": "page_changes", "target": "this page"}, ctx)
    assert "youtube.com changing" in res.content
    assert "download finishing" in (await registry.execute("list_watches", {}, ctx)).content
    res = await registry.execute("cancel_watch", {"which": "download"}, ctx)
    assert res.content == "Stopped watching for your download finishing."
    no_voice = await registry.execute("watch", {"kind": "download"}, ToolContext(settings))
    assert no_voice.is_error


@pytest.mark.parametrize("text,expected", [
    ("Tell me when my download finishes", ("watch", {"kind": "download"})),
    ("Can you tell me when my download finishes please", ("watch", {"kind": "download"})),
    ("tell me when my game finishes downloading", ("watch", {"kind": "steam_download"})),
    ("tell me when my gpu is below 60 degrees", ("watch", {"kind": "gpu_below", "value": 60.0})),
    ("warn me if my GPU goes above 85", ("watch", {"kind": "gpu_above", "value": 85.0})),
    ("tell me when the battery is full", ("watch", {"kind": "battery_above", "value": 100.0})),
    ("tell me when the internet is back", ("watch", {"kind": "internet_back"})),
    ("tell me when this page changes", ("watch", {"kind": "page_changes", "target": "this page"})),
    ("tell me when bbc.co.uk changes", ("watch", {"kind": "page_changes", "target": "bbc.co.uk"})),
    ("tell me when discord closes", ("watch", {"kind": "app_closes", "target": "discord"})),
    ("let me know when blender finishes rendering", ("watch", {"kind": "app_done", "target": "blender"})),
    ("what are you watching", ("list_watches", {})),
    ("stop watching the download", ("cancel_watch", {"which": "the download"})),
    ("When is dinner done?", None),                                  # a question, not a watch
])
def test_phrases(text, expected):
    assert match_intent(text) == expected
