"""Owner: "he still struggles doing simple tasks like open a new tab on my browser, I want him to
be able to control and navigate my browser and any app effortlessly". A pretend Firefox with
real tabs: keys only work when it really is in front, and every result is checked."""

import pytest

from assistant.brain.intents import match_intent
from assistant.tools import grid as G, keyboard as K, mybrowser as MB, pc, uia
from assistant.tools.registry import ToolContext, ToolError


class Firefox:
    """Windows + UI Automation + keyboard + mouse for one Firefox window with tabs."""

    def __init__(self, tabs=("YouTube", "Kelley Blue Book", "Spotify – Web Player"), front="discord",
                 focus_ok=True, keys_work=True):
        self.tabs = [{"title": t, "url": f"www.{t.split()[0].lower()}.com"} for t in tabs]
        self.sel, self.closed = 0, []
        self.front = front                  # which window has the keyboard: "firefox" / "discord" / "nova"
        self.focus_ok, self.keys_work = focus_ok, keys_work
        self.typed_into_bar, self.bar_active, self.pressed = "", False, []
        self.hud, self.discord = pc.Win(1, "Nova", "msedge.exe"), pc.Win(2, "Discord", "Discord.exe")

    # windows
    def win(self):
        t = self.tabs[self.sel]["title"] if self.tabs else ""
        return pc.Win(9, f"{t} — Mozilla Firefox", "firefox.exe")

    def list(self):
        order = {"nova": [self.hud, self.discord, self.win()], "discord": [self.discord, self.win(), self.hud],
                 "firefox": [self.win(), self.discord, self.hud]}[self.front]
        return order if self.tabs else [w for w in order if w.hwnd != 9]

    def active(self):
        return self.discord if self.front in ("discord", "nova") else self.win()

    def foreground(self):
        return {"nova": 1, "discord": 2, "firefox": 9}[self.front]

    def focus(self, hwnd):
        if self.focus_ok and hwnd == 9:
            self.front = "firefox"
        return self.focus_ok

    # UI Automation
    def tabs_of(self, hwnd):
        return [(t["title"], G.Region(100 + 200 * i, 5, 190, 30), i == self.sel) for i, t in enumerate(self.tabs)]

    def edit_values(self, hwnd):
        return [self.tabs[self.sel]["url"]] if self.tabs else []

    def elements(self, hwnd):
        return [uia.Element("Sign in", "button", G.Region(1500, 120, 80, 30)),
                uia.Element("2019 Honda Civic LX", "link", G.Region(300, 400, 200, 20))]

    # keyboard
    def combo(self, vks):
        keys = K.canonical(vks)
        self.pressed.append((keys, self.front))
        if self.front != "firefox" or not self.keys_work:
            return                                   # the keys went somewhere else
        tab = self.tabs[self.sel] if self.tabs else None
        if keys == "ctrl+t":
            self.tabs.append({"title": "New Tab", "url": ""})
            self.sel, self.bar_active = len(self.tabs) - 1, True
        elif keys == "ctrl+w":
            self.closed.append(self.tabs.pop(self.sel))
            self.sel = max(0, min(self.sel, len(self.tabs) - 1))
        elif keys == "ctrl+shift+t" and self.closed:
            self.tabs.append(self.closed.pop())
            self.sel = len(self.tabs) - 1
        elif keys == "ctrl+tab":
            self.sel = (self.sel + 1) % len(self.tabs)
        elif keys == "ctrl+shift+tab":
            self.sel = (self.sel - 1) % len(self.tabs)
        elif keys.startswith("ctrl+") and keys[-1].isdigit():
            n = int(keys[-1])
            self.sel = len(self.tabs) - 1 if n == 9 else min(n, len(self.tabs)) - 1
        elif keys == "ctrl+l":
            self.bar_active, self.typed_into_bar = True, ""
        elif keys == "enter" and self.bar_active and tab is not None:
            typed = self.typed_into_bar
            tab["url"] = typed
            tab["title"] = (typed.split("//")[-1].split("/")[0].removeprefix("www.") if "." in typed
                            else f"{typed} - Google Search")
            self.bar_active, self.typed_into_bar = False, ""

    def type(self, text):
        if self.front == "firefox" and self.bar_active:
            self.typed_into_bar += text

    # mouse (clicking a tab selects it)
    def move(self, x, y):
        self.at = (x, y)

    def click(self, button="left", double=False):
        for i in range(len(self.tabs)):
            if 100 + 200 * i <= self.at[0] <= 290 + 200 * i and self.at[1] < 40:
                self.sel = i


@pytest.fixture
def ff(monkeypatch):
    fake = Firefox()
    install(monkeypatch, fake)
    return fake


def install(monkeypatch, fake):
    monkeypatch.setattr(pc, "WINDOWS", fake)
    monkeypatch.setattr(uia, "UIA", type("U", (), {"tabs": lambda s, h: fake.tabs_of(h),
                                                   "edit_values": lambda s, h: fake.edit_values(h),
                                                   "elements": lambda s, h: fake.elements(h)})())
    monkeypatch.setattr(K, "KEYBOARD", fake)


def ctx_for(settings, fake):
    return ToolContext(settings, services={"grid": G.GridController(mouse=fake, overlay=type("O", (), {"hide": lambda s: None})())})


async def test_owners_case_new_tab_while_discord_is_in_front(settings, ff):
    """The keys used to go to Discord. Firefox is brought forward first, and the new tab is counted."""
    assert match_intent("Open a new tab on my browser") == ("my_browser", {"action": "new_tab"})
    out = await MB.my_browser({"action": "new_tab"}, ctx_for(settings, ff))
    assert out == "Opened a new tab in Firefox." and len(ff.tabs) == 4
    assert ff.pressed == [("ctrl+t", "firefox")]                     # never pressed in Discord


async def test_new_tab_when_nova_is_in_front(settings, ff):
    ff.front = "nova"
    out = await MB.my_browser({"action": "new_tab"}, ctx_for(settings, ff))
    assert out == "Opened a new tab in Firefox." and ff.pressed == [("ctrl+t", "firefox")]


async def test_windows_refusing_to_switch_is_said_not_hidden(settings, monkeypatch):
    fake = Firefox(focus_ok=False)
    install(monkeypatch, fake)
    with pytest.raises(ToolError, match="Windows wouldn't let me switch to Firefox"):
        await MB.my_browser({"action": "new_tab"}, ctx_for(settings, fake))
    assert fake.pressed == []                                         # nothing pressed blind


async def test_keys_that_do_nothing_are_not_claimed(settings, monkeypatch):
    fake = Firefox(keys_work=False)
    install(monkeypatch, fake)
    with pytest.raises(ToolError, match="no new tab appeared"):
        await MB.my_browser({"action": "new_tab"}, ctx_for(settings, fake))


async def test_new_tab_even_when_already_on_a_new_tab(settings, monkeypatch):
    """The title doesn't change ('New Tab' -> 'New Tab'); the tab count does."""
    fake = Firefox(tabs=("New Tab",), front="firefox")
    install(monkeypatch, fake)
    assert await MB.my_browser({"action": "new_tab"}, ctx_for(settings, fake)) == "Opened a new tab in Firefox."


async def test_open_a_site_in_a_new_tab(settings, ff):
    assert match_intent("open youtube in a new tab") == ("my_browser", {"action": "new_tab", "go": "youtube"})
    out = await MB.my_browser({"action": "new_tab", "go": "kelley blue book"}, ctx_for(settings, ff))
    assert out == "Opened kbb.com in a new tab in Firefox." and ff.tabs[-1]["url"] == "https://kbb.com"


async def test_go_and_search_in_this_tab(settings, ff):
    out = await MB.my_browser({"action": "go", "go": "reddit"}, ctx_for(settings, ff))
    assert out == "Opened reddit.com in Firefox." and len(ff.tabs) == 3
    out = await MB.my_browser({"action": "go", "go": "best gaming chair"}, ctx_for(settings, ff))
    assert out == "Searched for best gaming chair in Firefox."


async def test_switch_tab_by_name_and_number(settings, ff):
    assert match_intent("go to the spotify tab") == ("my_browser", {"action": "switch_tab", "tab": "spotify"})
    out = await MB.my_browser({"action": "switch_tab", "tab": "spotify"}, ctx_for(settings, ff))
    assert out.startswith("Now on Spotify") and ff.sel == 2
    out = await MB.my_browser({"action": "switch_tab", "number": 2}, ctx_for(settings, ff))
    assert out == "Now on Kelley Blue Book." and ff.sel == 1
    with pytest.raises(ToolError, match="can't see a netflix tab.*YouTube"):
        await MB.my_browser({"action": "switch_tab", "tab": "netflix"}, ctx_for(settings, ff))


async def test_switch_tab_without_the_tab_list(settings, ff, monkeypatch):
    """Some browsers don't list their tabs: Nova goes through them with Ctrl+Tab, reading titles."""
    monkeypatch.setattr(uia, "UIA", type("U", (), {"tabs": lambda s, h: [], "edit_values": lambda s, h: [],
                                                   "elements": lambda s, h: []})())
    out = await MB.my_browser({"action": "switch_tab", "tab": "kelley blue book"}, ctx_for(settings, ff))
    assert out == "Now on Kelley Blue Book." and ff.sel == 1


async def test_close_tab_by_name_and_reopen(settings, ff):
    out = await MB.my_browser({"action": "close_tab", "tab": "youtube"}, ctx_for(settings, ff))
    assert out == "Closed YouTube." and [t["title"] for t in ff.tabs] == ["Kelley Blue Book", "Spotify – Web Player"]
    out = await MB.my_browser({"action": "reopen_tab"}, ctx_for(settings, ff))
    assert out == "Brought back YouTube." and len(ff.tabs) == 3


async def test_list_tabs(settings, ff):
    out = await MB.my_browser({"action": "list_tabs"}, ctx_for(settings, ff))
    assert out.startswith("3 tabs open: 1, YouTube (this one); 2, Kelley Blue Book")


async def test_click_a_link_on_the_page(settings, ff):
    ff.front = "firefox"
    out = await MB.my_browser({"action": "click", "text": "honda civic"}, ctx_for(settings, ff))
    assert out.startswith("Clicked 2019 Honda Civic LX") and ff.at == (400, 410)


async def test_no_browser_open(settings, monkeypatch):
    fake = Firefox(tabs=())
    install(monkeypatch, fake)
    with pytest.raises(ToolError, match="can't see your browser"):
        await MB.my_browser({"action": "new_tab"}, ctx_for(settings, fake))


async def test_new_tab_shortcut_pressed_in_firefox_goes_through_the_checks(settings, registry, ff):
    """'press control t' with Firefox in front uses the same checked path."""
    ff.front = "firefox"
    res = await registry.execute("press_keys", {"keys": "ctrl+t"}, ctx_for(settings, ff))
    assert res.content == "Opened a new tab in Firefox." and len(ff.tabs) == 4


@pytest.mark.parametrize("said,expected", [
    ("new tab", {"action": "new_tab"}), ("Can you open a new tab in Firefox?", {"action": "new_tab"}),
    ("open another tab", {"action": "new_tab"}), ("open a new tab and go to reddit", {"action": "new_tab", "go": "reddit"}),
    ("close this tab", {"action": "close_tab"}), ("close the youtube tab", {"action": "close_tab", "tab": "youtube"}),
    ("close tab 2", {"action": "close_tab", "number": 2}), ("switch to tab 3", {"action": "switch_tab", "number": 3}),
    ("go to the second tab", {"action": "switch_tab", "number": 2}), ("next tab", {"action": "next_tab"}),
    ("reopen the last tab", {"action": "reopen_tab"}), ("what tabs do i have open", {"action": "list_tabs"}),
    ("find price on this page", {"action": "find", "text": "price"}), ("go back in my browser", {"action": "back"}),
    ("go to youtube in this tab", {"action": "go", "go": "youtube"}),
])
def test_phrases(said, expected):
    assert match_intent(said) == ("my_browser", expected)
