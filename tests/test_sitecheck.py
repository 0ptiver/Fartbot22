"""Owner: "you need to make it fact check what the real url is before it goes to it, it just
opened this scam website like 50 times" (kbbb.com instead of kbb.com, Kelley Blue Book)."""

import pytest

from assistant.brain.intents import match_intent
from assistant.brain.llm import TextDelta
from assistant.brain.local import LocalBrain, _prohibition, _small_talk
from assistant.core.conversation import Conversation
from assistant.tools import build_registry, mybrowser as MB, pc, sitecheck as S
from assistant.tools.registry import ToolContext, ToolError
from tests.test_local_brain import collect, make
from tests.test_mybrowser import Firefox, ctx_for, install


@pytest.mark.parametrize("fake,real", [("kbbb.com", "kbb.com"), ("kbb.co", "kbb.com"), ("youtub.com", "youtube.com"),
                                       ("gooogle.com", "google.com"), ("g00gle.com", "google.com"),
                                       ("paypa1.com", "paypal.com"), ("amazom.com", "amazon.com"),
                                       ("netflx.com", "netflix.com"), ("facebok.com", "facebook.com")])
def test_lookalikes_go_to_the_real_site(fake, real):
    url, note = S.check(fake)
    assert url == f"https://{real}" and fake in note and "lookalike" in note


@pytest.mark.parametrize("ok", ["kbb.com", "amazon.co.uk", "nbc.com", "abc.com", "ping.com", "bbc.co.uk",
                                "news.google.com", "youtubekids.com", "zara.com", "x.com", "reddit.com"])
def test_real_sites_are_left_alone(ok):
    assert S.check(ok) == (ok, "")


async def test_owners_case_kbbb_in_firefox_opens_the_real_kbb(settings, monkeypatch):
    fake = Firefox(front="firefox")
    install(monkeypatch, fake)
    out = await MB.my_browser({"action": "go", "go": "kbbb.com"}, ctx_for(settings, fake))
    assert out.startswith("kbbb.com isn't Kelley Blue Book") and fake.tabs[0]["url"] == "https://kbb.com"


def test_default_browser_path_checks_too(settings):
    opened = []
    out = pc.open_website({"site": "kbbb.com"}, ToolContext(settings), _open=opened.append)
    assert opened == ["https://kbb.com"] and "lookalike" in out
    out = pc.open_website({"site": "some car value site"}, ToolContext(settings), _open=opened.append)
    assert opened[-1].startswith("https://www.google.com/search?q=") and "searched" in out   # never a guessed address


def test_banned_sites_are_never_opened(settings):
    S.block("kbbb.com")
    S.block("sketchy.example")
    with pytest.raises(ToolError, match="never to open sketchy.example"):
        pc.open_website({"site": "sketchy.example"}, ToolContext(settings), _open=lambda u: None)
    assert match_intent("unblock sketchy.example") == ("block_site", {"action": "unblock", "site": "sketchy.example"})
    assert S.block_site({"action": "unblock", "site": "sketchy.example"}, None) == "Unblocked sketchy.example."


async def test_owners_case_stop_taking_me_there_does_nothing_and_blocks_it(local_settings, ctx):
    """'No stop. Stop taking me to that website Nova.' / 'Don't take me to that website ever again':
    the small model re-ran its last call for each. Now no model, no tool, and the site is banned."""
    brain, fake = make(local_settings, [])
    S.LAST_OPENED["host"] = "sketchy.example"
    conv = Conversation()
    for said in ["No stop. Stop taking me to that website Nova.", "Don't take me to that website ever again",
                 "Do not take me to that website ever again. That's not the URL you jackass."]:
        events = await collect(brain, conv, said, ctx)
        text = "".join(e.text for e in events if isinstance(e, TextDelta))
        assert "won't open sketchy.example again" in text
    assert fake.requests == [] and "sketchy.example" in S.blocked()


def test_prohibitions_vs_commands():
    assert _prohibition("don't do that again") == "Understood{sir}. I won't."
    assert _prohibition("stop the music") is None                  # a command
    assert _prohibition("never mind") is None
    assert _prohibition("open youtube") is None


def test_just_the_name_is_yes_sir():
    """Typing 'Nova' made the model repeat 'Kelly Blue Book is now loaded'."""
    assert _small_talk("Nova", ", sir") == "Yes, sir?"


def test_owners_case_new_tab_and_a_site_in_two_questions(settings, ctx):
    """'Can you go ahead and open a new tab for me? Can you open Kelly Blue Book?' searched for
    'me? can you open kelly blue book'."""
    brain = LocalBrain(settings, build_registry(settings))
    calls = brain._compound("Can you go ahead and open a new tab for me? Can you open Kelly Blue Book?", ctx)
    assert calls == [{"tool": "my_browser", "args": {"action": "new_tab", "go": "kelly blue book"}}]
    assert MB.destination("kelly blue book") == ("https://kbb.com", True)
    assert match_intent("open the website kelly blue book") == ("open_website", {"site": "kelly blue book"})
