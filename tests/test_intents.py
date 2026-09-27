import pytest

from assistant.brain.intents import match_intent


@pytest.mark.parametrize("text,expected", [
    ("What's playing?", ("now_playing", {})),
    ("I'm playing a song, tell me what's playing", ("now_playing", {})),   # the owner's words
    ("tell me what's playing", ("now_playing", {})),
    ("What song is this?", ("now_playing", {})),
    ("Alright I'm playing a song right now. What am I'm playing?", ("now_playing", {})),   # owner, verbatim
    ("What am I playing", ("now_playing", {})),
    ("what's this song called", ("now_playing", {})),
    ("Pause.", ("music_control", {"action": "pause"})),
    ("pause the music please", ("music_control", {"action": "pause"})),
    ("Stop the music.", ("music_control", {"action": "pause"})),
    ("Next song.", ("music_control", {"action": "next"})),
    ("Skip", ("music_control", {"action": "next"})),
    ("Play the next song", ("music_control", {"action": "next"})),
    ("Go back", ("music_control", {"action": "previous"})),
    ("Resume the music", ("music_control", {"action": "resume"})),
    ("Play music", ("music_control", {"action": "resume"})),
    ("Play My Way by Kanye West.", ("play_music", {"query": "my way by kanye west", "kind": "auto"})),
    ("Can you play some Drake please", ("play_music", {"query": "drake", "kind": "auto"})),
    ("Play my liked songs", ("play_music", {"kind": "liked_songs"})),
    ("Play the last song I listened to", ("play_music", {"kind": "recently_played"})),
    ("Play my chill playlist", ("play_music", {"query": "chill", "kind": "playlist"})),
    ("What's on my screen?", None),
    ("Open Spotify", None),
    ("What time is it", None),
    ("Stop the timer", ("cancel_timer", {"which": "timer"})),
])
def test_match_intent(text, expected):
    assert match_intent(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("Set a timer for 5 minutes.", ("set_timer", {"minutes": 5.0})),
    ("Set a timer for ten minutes for the pasta", ("set_timer", {"minutes": 10.0, "label": "pasta"})),
    ("Twenty minute timer", ("set_timer", {"minutes": 20.0})),
    ("timer for 30 seconds", ("set_timer", {"seconds": 30.0})),
    ("Start a 2 hour timer", ("set_timer", {"hours": 2.0})),
    ("Cancel the timer", ("cancel_timer", {"which": "timer"})),
    ("Stop the alarm", ("cancel_timer", {"which": "alarm"})),
    ("How much time is left?", ("list_timers", {})),
    ("How long left on the timer", ("list_timers", {})),
    ("Remind me in an hour to call mum", None),        # the model handles reminders
])
def test_timer_intents(text, expected):
    assert match_intent(text) == expected
