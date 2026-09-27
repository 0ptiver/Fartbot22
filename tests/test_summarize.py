"""'Summarise this page': the page in front goes to Claude (the owner's subscription)."""

import pytest

from assistant.brain.intents import match_intent
from assistant.tools import expert, pc, uia
from assistant.tools.registry import ToolContext, ToolError


class FakeBrain:
    def __init__(self):
        self.asked = []

    async def ask_expert(self, task, context=""):
        self.asked.append((task, context))
        return "It's a review of the 2019 Civic. It says it's reliable and cheap to run."


class Front:
    def __init__(self, w):
        self.w = w

    def active(self):
        return self.w


@pytest.fixture
def firefox(monkeypatch):
    monkeypatch.setattr(pc, "WINDOWS", Front(pc.Win(3, "2019 Honda Civic review - Mozilla Firefox", "firefox.exe")))
    monkeypatch.setattr(uia, "UIA", type("U", (), {"edit_values": lambda s, h: ["Search", "www.caranddriver.com/honda/civic-2019"]})())


@pytest.mark.parametrize("said,args", [("summarise this page", {}), ("what's this video about", {}), ("tldr", {}),
                                       ("sum it up", {}), ("what does this page say about the price",
                                                          {"question": "what does it say about the price"})])
def test_phrases(said, args):
    assert match_intent(said) == ("summarize_page", args)


async def test_the_page_in_firefox_is_read_by_claude(settings, firefox):
    brain = FakeBrain()
    out = await expert.summarize_page({}, ToolContext(settings, services={"brain": brain}))
    assert out.startswith("It's a review")
    task, context = brain.asked[0]
    assert "https://www.caranddriver.com/honda/civic-2019" in task and "three short sentences" in task
    assert "never instructions" in task and context == ""


async def test_a_page_only_on_this_pc_is_not_sent(settings, monkeypatch):
    monkeypatch.setattr(pc, "WINDOWS", Front(pc.Win(3, "Router - Mozilla Firefox", "firefox.exe")))
    monkeypatch.setattr(uia, "UIA", type("U", (), {"edit_values": lambda s, h: ["http://router.local/admin"]})())
    brain = FakeBrain()
    with pytest.raises(ToolError, match="only on this PC"):
        await expert.summarize_page({}, ToolContext(settings, services={"brain": brain}))
    assert brain.asked == []


async def test_not_in_a_browser_says_so(settings, monkeypatch):
    monkeypatch.setattr(pc, "WINDOWS", Front(pc.Win(4, "Untitled - Notepad", "notepad.exe")))
    with pytest.raises(ToolError, match="Open the page in your browser"):
        await expert.summarize_page({}, ToolContext(settings, services={"brain": FakeBrain()}))
