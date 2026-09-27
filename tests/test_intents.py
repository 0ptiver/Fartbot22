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
    ("Pause.", ("media", {"action": "pause"})),
    ("pause the music please", ("media", {"action": "pause"})),
    ("Stop the music.", ("media", {"action": "pause"})),
    ("Next song.", ("media", {"action": "next"})),
    ("Skip", ("media", {"action": "next"})),
    ("Play the next song", ("media", {"action": "next"})),
    ("Go back a song", ("media", {"action": "previous"})),
    ("Go back", ("press_keys", {"keys": "alt+left"})),              # browser back now; songs: "go back a song"
    ("Resume the music", ("media", {"action": "play"})),
    ("Play music", ("media", {"action": "play"})),
    ("Play My Way by Kanye West.", ("play_music", {"query": "my way by kanye west", "kind": "auto"})),
    ("Can you play some Drake please", ("play_music", {"query": "drake", "kind": "auto"})),
    ("Play my liked songs", ("play_music", {"kind": "liked_songs"})),
    ("Play the last song I listened to", ("play_music", {"kind": "recently_played"})),
    ("Play my chill playlist", ("play_music", {"query": "chill", "kind": "playlist"})),
    ("What's on my screen?", None),
    ("Open Spotify", ("open_app", {"name": "spotify"})),     # everyday commands skip the model now
    ("How do I lock my PC", None),
    ("Shut down the PC", None),                      # power always goes via the model + a yes/no
    ("What time is it", ("get_time", {})),
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
    ("Lock my PC.", ("lock_pc", {})),
    ("Lock the computer please", ("lock_pc", {})),
    ("Cancel the shutdown!", ("cancel_shutdown", {})),
    ("Show the desktop", ("window", {"action": "show_desktop"})),
    ("How much time is left?", ("list_timers", {})),
    ("How long left on the timer", ("list_timers", {})),
    ("Remind me in an hour to call mum", None),        # the model handles reminders
])
def test_timer_intents(text, expected):
    assert match_intent(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("volume up", ("volume", {"action": "up"})),
    ("turn it down a bit", ("volume", {"action": "down"})),
    ("turn the volume down a bit", ("volume", {"action": "down"})),
    ("set the volume to 30", ("volume", {"action": "set", "level": 30})),
    ("mute", ("volume", {"action": "mute"})),
    ("open up steam", ("open_app", {"name": "steam"})),
    ("launch youtube", ("open_website", {"site": "youtube"})),
    ("close discord", ("window", {"action": "close", "app": "discord"})),
    ("close it", ("window", {"action": "close", "app": "this"})),
    ("minimise this window", ("window", {"action": "minimize", "app": "this"})),
    ("switch to firefox", ("window", {"action": "focus", "app": "firefox"})),
    ("open my documents folder", None),                 # files: the model decides
    ("open discord and play some music", None),         # two things: the model
    ("start gaming mode", None),                        # a routine, not an app
    ("what's the weather", None),
])
def test_everyday_commands_skip_the_model(text, expected):
    """Owner: "he talks a ton instead of just doing". The commonest commands are done
    directly and answered with the tool's own short result."""
    assert match_intent(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("play it", ("media", {"action": "play"})),                  # not a song called "it"
    ("play", ("media", {"action": "play"})),
    ("put on some drake", ("play_music", {"query": "drake", "kind": "auto"})),
    ("play something by drake", ("play_music", {"query": "drake", "kind": "auto"})),
    ("put on my discover weekly playlist", ("play_music", {"query": "discover weekly", "kind": "playlist"})),
])
def test_music_phrasings(text, expected):
    assert match_intent(text) == expected


def test_open_spotify_and_play_is_one_command():
    """Owner's case: 'open spotify and play my way by kanye west' went to the model, which
    opened Spotify, failed to play, and then gave made-up advice."""
    assert match_intent("I want you to open spotify and play my way by Kanye West") == \
        ("play_music", {"query": "my way by kanye west", "kind": "auto"})
