import numpy as np
import pytest

from assistant.brain.llm import Brain
from assistant.core.config import WakeConfig
from assistant.voice.audio import RecordingPlayer
from assistant.voice.pipeline import VoiceLoop
from assistant.voice.wake import echo_overlap, match_wake
from tests.fakes import FakeClient, text_msg
from tests.voice_helpers import FakeSTT, FakeTTS

V = WakeConfig().variants


@pytest.mark.parametrize("heard,rest", [
    ("Nova, what time is it?", "What time is it?"),
    ("Hey Nova, open Spotify.", "Open Spotify."),
    ("What's the weather tomorrow, Nova?", "What's the weather tomorrow"),
    ("Okay, Nova. Set volume to 30.", "Set volume to 30."),
    ("Novo, mute.", "Mute."),
    ("No va, what's up", "What's up"),
    ("Nova.", ""),
])
def test_addressed(heard, rest):
    assert match_wake(heard, V) == (True, rest)


@pytest.mark.parametrize("heard", [
    "What time is it?",
    "I was watching a really good Nova documentary about space last night with friends",
    "Supernova is a cool word",
])
def test_not_addressed(heard):
    assert match_wake(heard, V)[0] is False


def test_echo_overlap():
    assert echo_overlap("the time is ten fifty eight", "The time is 10:58 p.m., sir. The time is ten fifty eight.") == 1
    assert echo_overlap("open spotify", "Good evening, sir.") == 0


class Utterances:
    """Mic stand-in: each item is (text for FakeSTT, seconds of 'speech')."""

    def __init__(self, loop_ref, items, gap_frames=40):
        self.loop_ref, self.items, self.gap = loop_ref, items, gap_frames

    async def frames(self):
        import asyncio
        for text, _ in self.items:
            self.loop_ref[0].stt.text = text
            for i in range(20):                  # speech
                yield np.full(512, 0.3, np.float32), 0.0
                await asyncio.sleep(0)
            for i in range(self.gap):            # silence ends the turn
                yield np.zeros(512, np.float32), 0.0
                await asyncio.sleep(0)
            while self.loop_ref[0].busy:         # wait for the reply to finish
                await asyncio.sleep(0.001)
            self.loop_ref[0]._mute_until = 0     # skip the post-reply cooldown in tests

    def close(self):
        pass


def make_wake_loop(settings, registry, script, items):
    settings.voice.mode = "wake"
    settings.voice.vad.min_speech_ms = 32
    events, ref = [], [None]
    brain = Brain(settings, registry, FakeClient(script))
    loop = VoiceLoop(settings, brain, FakeSTT(), FakeTTS(), Utterances(ref, items), RecordingPlayer(),
                     lambda f: 0.9 if np.max(np.abs(f)) > 0.1 else 0.0, None, events.append)
    ref[0] = loop
    return loop, events


async def test_wake_mode_flow(settings, registry):
    loop, events = make_wake_loop(settings, registry,
        [text_msg("It is noon, sir."), text_msg("You're welcome.")],
        [("what time is it", 1),               # not addressed -> ignored
         ("Nova, what time is it?", 1),        # answered
         ("thanks", 1)])                       # follow-up window -> answered without name
    await loop.run()
    kinds = [(e["type"], e.get("text")) for e in events if e["type"] in ("ignored", "transcript")]
    assert kinds == [("ignored", "what time is it"), ("transcript", "What time is it?"),
                     ("transcript", "thanks")]
    assert loop.tts.spoken == ["It is noon, sir.", "You're welcome."]
    sent = loop.brain.client.messages.calls[0]["messages"][0]["content"][-1]["text"]
    assert sent == "What time is it?"          # the name is stripped before the model sees it


async def test_name_only_gets_acknowledgement(settings, registry):
    loop, events = make_wake_loop(settings, registry, [], [("Nova.", 1)])
    await loop.prewarm()
    await loop.run()
    assert "Yes, sir?" in loop.tts.spoken
    assert loop.brain.client.messages.calls == []


async def test_own_voice_is_ignored(settings, registry):
    loop, events = make_wake_loop(settings, registry, [text_msg("The time is ten fifty eight, sir.")],
        [("Nova what time is it", 1), ("the time is ten fifty eight sir", 1)])
    await loop.run()
    ign = [e for e in events if e["type"] == "ignored"]
    assert ign and ign[0]["reason"] == "sounded like my own voice"


async def test_tts_cache_reuses_short_phrases(settings, registry):
    loop, _ = make_wake_loop(settings, registry, [], [])
    await loop.prewarm()
    n = len(loop.tts.spoken)
    await loop.say("One moment, sir.")
    assert len(loop.tts.spoken) == n            # served from cache, not re-synthesized


async def test_follow_up_does_not_chain(settings, registry):
    loop, events = make_wake_loop(settings, registry,
        [text_msg("It is noon, sir."), text_msg("You're welcome, sir.")],
        [("Nova, what time is it?", 1),
         ("thanks", 1),                          # follow-up window -> answered
         ("man what is going on here", 1)])      # window doesn't chain -> ignored
    await loop.run()
    got = [(e["type"], e.get("text")) for e in events if e["type"] in ("ignored", "transcript")]
    assert got[-1] == ("ignored", "man what is going on here")
    assert loop.tts.spoken == ["It is noon, sir.", "You're welcome, sir."]


async def test_repeating_novas_words_with_name_is_not_echo(settings, registry):
    """Owner's case: Nova said 'Play my way by Kanye West...' and 'Nova play my way by Kanye
    West' was then ignored as 'my own voice'."""
    loop, events = make_wake_loop(settings, registry,
        [text_msg("Play my way by Kanye West, I can't find that track, sir."), text_msg("Playing it now, sir.")],
        [("Nova, play my way", 1),
         ("Nova play my way by Kanye West", 1)])
    await loop.run()
    assert not [e for e in events if e["type"] == "ignored"]
    assert loop.tts.spoken[-1] == "Playing it now, sir."


async def test_old_words_are_not_echo_after_the_window(settings, registry):
    loop, events = make_wake_loop(settings, registry, [text_msg("The time is noon, sir.")], [])
    loop._last_said, loop._spoke_at = "the time is noon sir", 0.0      # long ago
    loop._follow_up_until = float("inf")
    loop.cfg.wake.follow_up_smart = False                               # (this test is about echo)
    assert await loop._check_wake("the time is noon", None) == "the time is noon"


# Owner: "he answers everything I say when I don't say his name ... it gets quite annoying".
async def test_chatting_with_friends_after_a_reply_is_ignored(settings, registry):
    loop, events = make_wake_loop(settings, registry, [text_msg("It is noon, sir.")],
        [("Nova, what time is it?", 1),
         ("bro we need to hit the bank before the cops show up", 1)])      # GTA RP chat, in the window
    await loop.run()
    got = [(e["type"], e.get("reason")) for e in events if e["type"] in ("ignored", "transcript")]
    assert got[-1] == ("ignored", "didn't sound like it was for me")
    assert loop.tts.spoken == ["It is noon, sir."]


@pytest.mark.parametrize("follow", ["volume up", "and what about tomorrow", "thanks", "yes please"])
async def test_real_follow_ups_still_work_without_the_name(settings, registry, follow):
    loop, events = make_wake_loop(settings, registry, [text_msg("It is noon, sir."), text_msg("Done, sir.")],
        [("Nova, what time is it?", 1), (follow, 1)])
    await loop.run()
    assert ("transcript", follow) in [(e["type"], e.get("text")) for e in events]


async def test_any_answer_to_novas_question_counts(settings, registry):
    loop, events = make_wake_loop(settings, registry, [text_msg("Which one, sir?"), text_msg("Done, sir.")],
        [("Nova, open the game", 1), ("the one with the cars", 1)])
    await loop.run()
    assert ("transcript", "the one with the cars") in [(e["type"], e.get("text")) for e in events]


async def test_stand_down_from_the_game_doesnt_put_nova_to_sleep(settings, registry):
    """'Stand down!' is everyday GTA RP talk; hearing it put Nova to sleep (owner's case)."""
    loop, events = make_wake_loop(settings, registry, [], [("stand down, stand down, hands up", 1)])
    await loop.run()
    assert not loop.standby
    loop2, _ = make_wake_loop(settings, registry, [], [("Nova, stand down", 1)])
    await loop2.run()
    assert loop2.standby


def test_follow_on_words():
    from assistant.voice.wake import continues, follows_on, unfinished
    assert follows_on("and turn it up") and follows_on("Thanks.") and not follows_on("we need to go")
    assert unfinished("open a new tab and") and unfinished("what's the weather,") and not unfinished("open discord")
    assert continues("in Chicago tomorrow") and not continues("yo what's up guys")
