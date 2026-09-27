"""Nova learns from corrections, teaching and preferences (owner: "if I tell him to open a
website and he does it wrong I want to be able to correct him, like a self-learning AI")."""

from assistant.brain import lessons as L
from assistant.brain.llm import TextDelta
from assistant.core.conversation import Conversation
from tests.test_local_brain import collect, make, text_reply, tool_reply


def said(events):
    return "".join(e.text for e in events if isinstance(e, TextDelta))


def fake_sites(brain):
    opened = []

    def open_website(args, ctx):
        opened.append(args.get("site"))
        return f"Opened {args.get('site')}."
    brain.registry._tools["open_website"].handler = open_website
    return opened


async def test_a_correction_is_learned_and_used_next_time(local_settings, ctx):
    brain, fake = make(local_settings, [
        tool_reply("open_website", {"site": "the news"}), text_reply("The news is open, sir."),
        tool_reply("open_website", {"site": "bbc.co.uk"}), text_reply("BBC News is open, sir."),
    ])
    opened = fake_sites(brain)
    conv = Conversation()
    await collect(brain, conv, "get me the news", ctx)
    events = await collect(brain, conv, "no, I meant bbc.co.uk", ctx)
    assert said(events).endswith("Noted for next time.")
    assert "corrects my previous request" in fake.requests[2][1]["messages"][-1]["content"]
    # Next time: straight to the right site, no model needed.
    requests_before = len(fake.requests)
    events = await collect(brain, Conversation(), "Nova, could you get me the news please", ctx)
    assert opened == ["the news", "bbc.co.uk", "bbc.co.uk"]
    assert len(fake.requests) == requests_before
    assert said(events) == "Opened bbc.co.uk."
    lesson = L.get_lessons().items()[0]
    assert lesson.said == "get me the news" and lesson.calls == [{"tool": "open_website", "args": {"site": "bbc.co.uk"}}]
    assert lesson.uses == 1


async def test_thats_wrong_asks_what_it_should_have_done(local_settings, ctx):
    brain, fake = make(local_settings, [
        tool_reply("open_website", {"site": "news"}), text_reply("Done, sir."),
        tool_reply("open_website", {"site": "bbc.co.uk"}), text_reply("BBC is open."),
    ])
    opened = fake_sites(brain)
    conv = Conversation()
    await collect(brain, conv, "get me the news", ctx)
    events = await collect(brain, conv, "that's the wrong website", ctx)
    assert said(events) == "Sorry, sir. What should I have done?"
    events = await collect(brain, conv, "open bbc.co.uk", ctx)
    assert opened[-1] == "bbc.co.uk" and said(events).endswith("Noted for next time.")
    assert L.get_lessons().match("get me the news") is not None


async def test_never_mind_drops_the_question(local_settings, ctx):
    brain, fake = make(local_settings, [tool_reply("open_website", {"site": "news"}), text_reply("Done.")])
    fake_sites(brain)
    conv = Conversation()
    await collect(brain, conv, "get me the news", ctx)
    await collect(brain, conv, "that's wrong", ctx)
    events = await collect(brain, conv, "never mind", ctx)
    assert said(events) == "All right, sir." and L.get_lessons().items() == []


async def test_no_thanks_is_not_a_correction(local_settings, ctx):
    brain, fake = make(local_settings, [tool_reply("open_website", {"site": "news"}), text_reply("Done."),
                                        text_reply("Very good, sir.")])
    fake_sites(brain)
    conv = Conversation()
    await collect(brain, conv, "get me the news", ctx)
    events = await collect(brain, conv, "no thanks", ctx)
    assert said(events) == "Very good, sir." and L.get_lessons().items() == []


async def test_teaching_a_phrase(local_settings, ctx):
    brain, fake = make(local_settings, [])
    opened = fake_sites(brain)
    events = await collect(brain, Conversation(), "when I say the news, open bbc.co.uk", ctx)
    assert said(events) == "Got it. When you say “the news”, I'll open bbc.co.uk."
    events = await collect(brain, Conversation(), "the news", ctx)
    assert opened == ["bbc.co.uk"] and not fake.requests          # the fast path did it


async def test_preferences_are_remembered_without_asking(local_settings, ctx, tmp_path):
    from assistant.core.memory import MemoryStore
    store = MemoryStore(tmp_path / "m.db")
    ctx.services["memory"] = store
    brain, fake = make(local_settings, [])
    events = await collect(brain, Conversation(), "I prefer dark mode", ctx)
    assert said(events) == "Noted, sir. I'll remember that."
    assert [m.text for m in store.all()] == ["I prefer dark mode"] and not fake.requests


def test_what_counts_as_a_correction():
    assert L.correction("no, I meant bbc.co.uk") == (True, "bbc.co.uk")
    assert L.correction("wrong one, open youtube music") == (True, "open youtube music")
    assert L.correction("that's the wrong website") == (True, "")
    assert L.correction("you opened the wrong app, I wanted discord") == (True, "discord")
    assert L.correction("no thanks") == (False, "")
    assert L.correction("no way that's crazy") == (False, "")
    assert L.correction("open youtube") == (False, "")


def test_what_counts_as_a_preference():
    assert L.preference("my favourite team is Arsenal") == "My favourite team is Arsenal"
    assert L.preference("from now on always open youtube in firefox")
    assert L.preference("I like this song") is None                  # only makes sense right now
    assert L.preference("do I like pizza?") is None
    assert L.preference("I love you") is None


async def test_lessons_can_be_listed_and_forgotten(local_settings, ctx):
    from assistant.tools.learn import lessons
    store = L.get_lessons()
    store.learn_meaning("the news", "open bbc.co.uk", "when I say the news, open bbc.co.uk")
    store.learn_calls("play chill music", [{"tool": "play_music", "args": {"query": "lofi beats"}}], "no, lofi beats")
    out = await lessons({"action": "list"}, ctx)
    assert out.startswith("I've learned 2 things") and "lofi beats" in out
    assert "news" in await lessons({"action": "forget", "about": "the news"}, ctx)
    assert [x.said for x in store.items()] == ["play chill music"]
    await lessons({"action": "forget"}, ctx)
    assert store.items() == []


def test_lookups_are_not_learned():
    assert L.get_lessons().learn_calls("whats the time", [{"tool": "get_time", "args": {}}], "no") is None


async def test_what_have_you_learned_also_lists_shown_routines(ctx):
    """'What have you learned' used to list only routines taught by showing; it covers both now."""
    from assistant.brain.intents import match_intent
    from assistant.tools import teach
    from assistant.tools.learn import lessons
    assert match_intent("what have you learned") == ("lessons", {"action": "list"})
    teach.save_learned({"evening_setup": {"label": "Evening setup", "steps": []}})
    assert "Evening setup" in await lessons({"action": "list"}, ctx)


async def test_polite_openers_are_not_lessons(local_settings, ctx):
    """Owner: "it's barely responding, I tell it to do something and it doesn't do it".
    'Sorry, can you open the weather' right after another request was learned as a correction,
    so the first request started doing the wrong thing."""
    brain, fake = make(local_settings, [
        tool_reply("open_website", {"site": "the news"}), text_reply("Done."),
        tool_reply("open_website", {"site": "weather.com"}), text_reply("Done."),
    ])
    fake_sites(brain)
    conv = Conversation()
    await collect(brain, conv, "get me the news", ctx)
    await collect(brain, conv, "Sorry, can you get me the weather", ctx)
    assert L.get_lessons().items() == []
    assert L.correction("Actually, open steam") == (False, "")
    assert not L.explicit("No, open steam") and L.explicit("no, I meant steam")


async def test_correcting_a_learned_shortcut_drops_it(local_settings, ctx):
    brain, fake = make(local_settings, [tool_reply("open_website", {"site": "bbc.co.uk/sport"}), text_reply("Done.")])
    opened = fake_sites(brain)
    store = L.get_lessons()
    store.learn_calls("get me the news", [{"tool": "open_website", "args": {"site": "bbc.co.uk"}}], "no, I meant bbc.co.uk")
    conv = Conversation()
    await collect(brain, conv, "get me the news", ctx)                       # replays the lesson
    await collect(brain, conv, "no, I meant bbc.co.uk/sport", ctx)
    assert opened == ["bbc.co.uk", "bbc.co.uk/sport"]
    [lesson] = store.items()
    assert lesson.calls == [{"tool": "open_website", "args": {"site": "bbc.co.uk/sport"}}]


async def test_claims_after_fire_up_are_caught(local_settings, ctx):
    brain, fake = make(local_settings, [text_reply("Discord is now open, sir."),
                                        tool_reply("open_app", {"name": "discord"}), text_reply("Done, sir.")])
    brain.registry._tools["open_app"].handler = lambda a, c: "Discord is open."
    events = await collect(brain, Conversation(), "fire up discord", ctx)
    assert any(getattr(e, "name", None) == "open_app" for e in events)
    assert "now open" not in said(events)


async def test_nova_is_never_silent(local_settings, ctx):
    """An empty reply from the model used to mean no answer at all."""
    brain, fake = make(local_settings, [text_reply(""), tool_reply("open_app", {"name": "discord"}), text_reply("")])
    brain.registry._tools["open_app"].handler = lambda a, c: "Discord is open."
    assert said(await collect(brain, Conversation(), "tell me something", ctx)) == \
        "Sorry, sir, I didn't catch that. Could you say it again?"
    assert said(await collect(brain, Conversation(), "fire up discord", ctx)) == "Done."
