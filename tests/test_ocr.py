"""Nova reading the screen itself (owner: "give him actual real time access so he can flawlessly
do things and actually see what he is doing", without Claude). Windows' OCR is faked here."""

import pytest

from assistant.brain.llm import TextDelta, ToolFinished
from assistant.core.conversation import Conversation
from assistant.tools import ocr, pc, screen, uia
from assistant.tools.ocr import Line, Word
from tests.test_local_brain import FakeExpert, collect, make, text_reply


def line(text, x, y, word_w=60, h=20):
    words, cx = [], x
    for t in text.split():
        words.append(Word(t, cx, y, word_w, h))
        cx += word_w + 10
    return Line(text, words)


class FakeOcr:
    """A window at (100, 50); what it shows can change after a click."""

    def __init__(self, screens):
        self.screens = list(screens)
        self.reads = 0

    def capture(self, area, monitor=1):
        left, top = (area[0], area[1]) if area else (0, 0)
        return b"", 800, 600, left, top

    def read(self, bgra, w, h):
        self.reads += 1
        shown = self.screens[0] if len(self.screens) == 1 else self.screens.pop(0)
        return [Line(ln.text, [Word(wd.text, wd.x, wd.y, wd.w, wd.h) for wd in ln.words]) for ln in shown]


class FakeWindows:
    def active(self):
        return pc.Win(7, "FiveM - Main menu", "FiveM.exe")

    def rect(self, hwnd):
        return 100, 50, 800, 600

    def list(self):
        return [self.active()]

    def foreground(self):
        return 7


class FakeMouse:
    def __init__(self):
        self.moves, self.clicks = [], []

    def move(self, x, y):
        self.moves.append((x, y))

    def click(self, button="left", double=False):
        self.clicks.append((button, double))


MENU = [line("Main menu", 10, 10), line("Play Online", 10, 100), line("Settings", 10, 140),
        line("Quit Game", 10, 180)]
CONNECTING = [line("Connecting to server...", 10, 100)]


@pytest.fixture
def screen_now(monkeypatch):
    monkeypatch.setattr(pc, "WINDOWS", FakeWindows())
    monkeypatch.setattr(ocr, "SETTLE_S", 0)

    def show(*screens):
        fake = FakeOcr(screens)
        monkeypatch.setattr(ocr, "OCR", fake)
        return fake
    return show


def test_finds_words_where_they_are_even_misread_a_little():
    hits = ocr.find(MENU, "play online")
    assert hits[0].text == "Play Online" and hits[0].x == 10 + (60 + 10 + 60) // 2
    assert ocr.find(MENU, "setings")[0].text == "Settings"             # one letter off: still found
    assert ocr.find(MENU, "multiplayer") == []


def test_reads_the_window_in_front_top_to_bottom(screen_now):
    screen_now(list(reversed(MENU)))
    label, lines = ocr.read_screen()
    assert label == "FiveM" and [ln.text for ln in lines][0] == "Main menu"
    assert lines[1].words[0].x == 110 and lines[1].words[0].y == 150    # moved to screen pixels
    assert ocr.as_text(label, lines).startswith("Text in FiveM, top to bottom:\nMain menu\nPlay Online")


def test_clicks_what_is_written_and_checks_it_worked(screen_now):
    screen_now(MENU, CONNECTING)
    mouse = FakeMouse()
    assert ocr.click_text("play online", mouse) == "Clicked 'Play Online'."
    assert mouse.moves == [(110 + 65, 150 + 10)] and mouse.clicks == [("left", False)]


def test_says_so_when_a_click_changed_nothing(screen_now):
    """Never claim a click worked without looking (owner's rule)."""
    screen_now(MENU)
    assert "nothing on the screen changed" in ocr.click_text("settings", FakeMouse())


def test_click_by_name_falls_back_to_the_words_on_screen(screen_now, monkeypatch, ctx):
    """Games don't list their buttons for Windows: 'click play online' still works."""
    screen_now(MENU, CONNECTING)
    monkeypatch.setattr(uia, "UIA", type("U", (), {"elements": lambda self, hwnd: []})())
    from assistant.tools.grid import controller
    g = controller(ctx)
    g.mouse = FakeMouse()
    assert uia.click_element({"name": "Play Online"}, ctx) == "Clicked 'Play Online'."


def test_look_at_screen_reads_the_text_itself(screen_now, ctx):
    screen_now([line("Error: the file is in use by another program", 10, 40)])
    out = screen.look_at_screen({"focus": "what does this error say"}, ctx)
    assert isinstance(out, str) and "file is in use" in out


def test_a_picture_question_still_uses_the_picture(screen_now, ctx, monkeypatch):
    fake = screen_now(MENU)
    from PIL import Image
    monkeypatch.setattr(screen, "grab_screen", lambda m=1: Image.new("RGB", (40, 30)))
    out = screen.look_at_screen({"focus": "what colour is the logo"}, ctx)
    assert not isinstance(out, str) and fake.reads == 0


async def test_whats_on_my_screen_is_answered_by_nova_not_claude(screen_now, local_settings, ctx):
    screen_now([line("Error: the file is in use by another program", 10, 40)])
    expert = FakeExpert()
    brain, fake = make(local_settings, [text_reply("An error says the file is in use by another program, sir.")], expert)
    events = await collect(brain, Conversation(), "what's on my screen", ctx)
    said = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert said == "An error says the file is in use by another program, sir." and expert.calls == []
    fin = next(e for e in events if isinstance(e, ToolFinished))
    assert fin.name == "look_at_screen" and not fin.is_error
    sent = fake.requests[0][1]["messages"]
    assert any(m.get("role") == "tool" and "file is in use" in m.get("content", "") for m in sent)
