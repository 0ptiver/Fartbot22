import pytest

from assistant.brain.intents import match_intent


@pytest.mark.parametrize("text,expected", [
    ("What's playing?", ("now_playing", {})),
    ("I'm playing a song, tell me what's playing", ("now_playing", {})),   # the owner's words
    ("tell me what's playing", ("now_playing", {})),
    ("What song is this?", ("now_playing", {})),
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
    ("Stop the timer", None),
])
def test_match_intent(text, expected):
    assert match_intent(text) == expected
