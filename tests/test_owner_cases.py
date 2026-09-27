"""The owner's latest screenshots, as tests (owner: "you broke him")."""

import pytest

from assistant.brain.intents import match_intent
from assistant.brain.llm import TextDelta, ToolStarted
from assistant.core.conversation import Conversation
from assistant.tools import build_registry
from tests.test_local_brain import collect, make, text_reply, tool_reply


def said(events):
    return "".join(e.text for e in events if isinstance(e, TextDelta))


def tools(events):
    return [e.name for e in events if isinstance(e, ToolStarted)]


async def test_pause_the_video_and_open_spotify_is_done_directly(local_settings, ctx):
    """Screenshot: 'Pause the video and open spotify' -> 'Sorry, sir, I wasn't able to do that.'"""
    brain, fake = make(local_settings, [])
    brain.registry._tools["video"].handler = lambda a, c: "Paused."
    brain.registry._tools["open_app"].handler = lambda a, c: "Spotify is open."
    events = await collect(brain, Conversation(), "Pause the video and open spotify", ctx)
    assert tools(events) == ["video", "open_app"] and not fake.requests
    assert said(events) == "Paused. Spotify is open."


async def test_i_cant_open_a_browser_is_nudged(local_settings, ctx):
    """Screenshot: 'I cannot directly navigate to kbb.com or browse the site as part of this
    interaction.' It can: the refusal is never read out, and the model is told to use its tools."""
    brain, fake = make(local_settings, [
        text_reply("I cannot directly navigate to kbb.com or browse the site as part of this interaction."),
        tool_reply("open_app", {"name": "edge"}), text_reply("Done, sir."),
    ])
    brain.registry._tools["open_app"].handler = lambda a, c: "Edge is open."
    events = await collect(brain, Conversation(), "go through the listings on that car site for me", ctx)
    assert "cannot" not in said(events)
    assert "you CAN do this" in fake.requests[1][1]["messages"][-1]["content"]


def test_owners_kbb_phrases_go_to_the_browser():
    got = match_intent("Go to kbb.com and look at the resale value of a 2016 Honda Accord")
    assert got == ("browser", {"action": "search", "site": "kbb.com", "text": "2016 honda accord", "details": False})
    assert match_intent("what's a 2019 honda civic worth on kelley blue book")[0] == "browser"


async def test_open_edge_and_look_searches_what_was_just_asked(local_settings, ctx):
    """Screenshot: 'but why don't you open edge and look' -> 'I cannot open Edge or any browser'."""
    brain, fake = make(local_settings, [tool_reply("web_search", {"query": "2019 Honda Civic value"}),
                                        text_reply("About 18,000 dollars.")])
    brain.registry._tools["web_search"].handler = lambda a, c: "Some results."
    seen = []

    async def browser(a, c):
        seen.append(a)
        return "Searched for it."
    brain.registry._tools["browser"].handler = browser
    conv = Conversation()
    await collect(brain, conv, "how much is a 2019 honda civic worth these days", ctx)
    events = await collect(brain, conv, "but why don't you open edge and look", ctx)
    assert seen == [{"action": "search", "text": "2019 Honda Civic value", "details": False}]
    assert said(events) == "Searched for it."


def test_the_model_chooses_from_fewer_tools(settings):
    """54 tools made the small model sloppy; the ones said directly are hidden from it."""
    reg = build_registry(settings)
    shown = [n for n, t in reg._tools.items() if not t.internal]
    assert len(shown) <= 32
    assert "browser" in shown and "open_app" in shown and "media" in shown
    assert reg._tools["lock_pc"].handler is not None                    # hidden, still works directly
    assert match_intent("lock my pc") == ("lock_pc", {})


@pytest.mark.parametrize("text", ["search for salt and pepper", "tom and jerry", "full screen the video and play it"])
def test_compounds_only_when_every_part_is_a_command(text, settings, ctx):
    from assistant.brain.local import LocalBrain
    brain = LocalBrain(settings, build_registry(settings))
    assert brain._compound(text, ctx) is None


async def test_you_didnt_do_it_redoes_it(local_settings, ctx):
    """Screenshot: 'Hello? Are you even listening to me? I just told you that you didn't close my
    video and you didn't open Spotify!' -> 'Sorry, sir, I wasn't able to do that.'"""
    brain, fake = make(local_settings, [])
    brain.registry._tools["video"].handler = lambda a, c: f"Video: {a['actions'][0]}."
    brain.registry._tools["open_app"].handler = lambda a, c: "Spotify is open."
    events = await collect(brain, Conversation(), "Hello? Are you even listening to me? I just told you that you "
                                                  "didn't close my video and you didn't open Spotify!", ctx)
    assert tools(events) == ["video", "open_app"] and not fake.requests
    assert said(events) == "Video: pause. Spotify is open."


def test_close_my_video_pauses_it():
    """The model played the video when asked to close it; closing means pausing (safe)."""
    assert match_intent("close my video") == ("video", {"actions": ["pause"]})
    assert match_intent("exit the video") == ("video", {"actions": ["pause"]})
    assert match_intent("close youtube")[0] == "window"             # the app/tab itself: still closes it


def test_a_complaint_that_isnt_a_command_goes_to_the_model(settings, ctx):
    from assistant.brain.local import LocalBrain
    assert LocalBrain(settings, build_registry(settings))._complaint("you didn't tell me the truth", ctx) is None


def test_nova_knows_oliver_made_it(settings):
    """Owner: 'make it understand that I, Oliver, made him and he does what I tell him'."""
    from assistant.brain.local import _small_talk
    from assistant.brain.prompts import system_prompt
    p = system_prompt(settings, local=True)
    assert "made by Oliver" in p and "Don't question, second-guess or lecture" in p
    assert _small_talk("who made you", ", sir", "Oliver") == "Oliver did, sir. I work for Oliver."
    assert _small_talk("who's your boss", ", sir", "Oliver") == "Oliver, sir."


async def test_situation_is_given_to_the_model(local_settings, ctx, monkeypatch):
    """Owner: 'he doesn't know what's going on, when he's selected onto the browser'. The model
    is told which window is in front (where keys go) before it answers."""
    from assistant.brain import situation

    async def now():
        return {"window in front (keys and typing go here)": "Firefox: YouTube — Mozilla Firefox"}
    monkeypatch.setattr(situation, "situation", now)
    brain, fake = make(local_settings, [text_reply("It's YouTube in Firefox, sir.")])
    await collect(brain, Conversation(), "which app am i in right now", ctx)
    assert "Firefox: YouTube" in fake.requests[0][1]["messages"][-1]["content"]


async def test_new_tab_in_novas_browser_is_done_directly(monkeypatch, settings, registry):
    """Keys can't reach a browser's tab bar from outside; in Nova's browser it's done directly."""
    from assistant.tools import browser as B
    from assistant.tools.registry import ToolContext
    monkeypatch.setattr(B.BROWSER, "in_front", lambda: True)
    done = []

    async def shortcut(combo):
        done.append(combo)
        return "Opened a new tab in my browser."
    monkeypatch.setattr(B, "shortcut", shortcut)
    res = await registry.execute("press_keys", {"keys": "ctrl+t"}, ToolContext(settings))
    assert res.content == "Opened a new tab in my browser." and done == ["ctrl+t"]
