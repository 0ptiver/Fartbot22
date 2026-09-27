"""Long-term memory: store, privacy rules, voice phrases, and the model seeing relevant facts."""

import pytest

from assistant.brain.intents import match_intent
from assistant.core.conversation import Conversation
from assistant.core.memory import MemoryStore, SecretRefused
from assistant.tools.registry import ToolContext
from tests.test_local_brain import collect, make, text_reply


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.db")


def test_add_update_and_search(store):
    store.add("My sister's birthday is June 3")
    store.add("I like pizza")
    store.add("I like sushi")                                        # a second like, not a replacement
    mem, replaced = store.add("my sister's birthday is June 4")      # same fact, new value
    assert replaced and len(store.all()) == 3
    assert [m.text for m in store.search("when is my sister's birthday")] == ["my sister's birthday is June 4"]
    assert {m.text for m in store.search("what food do I like")} == {"I like pizza", "I like sushi"}


@pytest.mark.parametrize("secret", ["The wifi password is hunter2", "my PIN is 4432",
                                    "card number 4111 1111 1111 1111", "my bank account is 12345678 sort code 12-34-56"])
def test_secrets_are_refused(store, secret):
    with pytest.raises(SecretRefused):
        store.add(secret)
    assert store.all() == []


def test_persists_on_disk(tmp_path):
    a = MemoryStore(tmp_path / "m.db")
    a.add("My dog is called Max")
    a.close()
    assert [m.text for m in MemoryStore(tmp_path / "m.db").all()] == ["My dog is called Max"]


def test_for_turn_sends_only_relevant_ones_when_there_are_many(store):
    for fact in ["I like pizza", "My name is Ollie", "My sister lives in Denver", "I hate mushrooms",
                 "My favourite game is GTA V", "I work out on Mondays", "I prefer dark mode",
                 "My mum's name is Karen", "My laptop has an RTX 5070", "I usually game after 9 pm"]:
        store.add(fact)
    store.add("My dog is called Max")
    assert store.for_turn("what's my dog called") == ["My dog is called Max"]


async def test_tools_and_forget_asks_first(settings, registry):
    ctx = ToolContext(settings)
    res = await registry.execute("remember", {"text": "My dog is called Max"}, ctx)
    assert res.content == "Remembered: My dog is called Max."
    bad = await registry.execute("remember", {"text": "my password is hunter2"}, ctx)
    assert bad.is_error and "password manager" in bad.content
    assert "Max" in (await registry.execute("recall", {}, ctx)).content
    asked = []

    async def no(tool, args):
        asked.append(registry.describe(tool, args))
        return False
    kept = await registry.execute("forget", {"what": "dog"}, ToolContext(settings, confirm=no))
    assert kept.is_error and asked == ['forget "My dog is called Max"']

    async def yes(*a):
        return True
    gone = await registry.execute("forget", {"what": "dog"}, ToolContext(settings, confirm=yes))
    assert gone.content == 'Forgotten: "My dog is called Max".'
    remote = await registry.execute("forget", {"what": "everything"}, ToolContext(settings, remote=True))
    assert remote.is_error and "blocked" in remote.content


async def test_the_model_sees_relevant_memories(settings, ctx):
    settings.brain.backend = "local"
    brain, fake = make(settings, [text_reply("It's June 3rd, sir.")])
    from assistant.core.memory import get_store
    get_store(settings).add("My sister's birthday is June 3")
    await collect(brain, Conversation(), "when is my sister's birthday?", ctx)
    sent = fake.requests[0][1]["messages"][-1]["content"]
    assert "remember" in sent and "My sister's birthday is June 3" in sent


@pytest.mark.parametrize("text,expected", [
    ("Remember that my sister's birthday is June 3rd.", ("remember", {"text": "my sister's birthday is June 3rd"})),
    ("Don't forget that I hate mushrooms", ("remember", {"text": "I hate mushrooms"})),
    ("Remember to buy milk", None),                                   # a reminder, not a fact: the model decides
    ("What do you remember?", ("recall", {})),
    ("what do you know about me", ("recall", {})),
    ("what do you remember about my sister", ("recall", {"about": "my sister"})),
    ("forget that", ("forget", {"what": "that"})),
    ("forget everything", ("forget", {"what": "everything"})),
])
def test_phrases(text, expected):
    assert match_intent(text) == expected
