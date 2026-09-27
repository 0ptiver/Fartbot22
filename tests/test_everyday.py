"""Owner: "Nova is still struggling to answer me on simple commands". The everyday things
people say are done directly (no model, no chatter); this table guards where each one goes."""

import httpx
import pytest

from assistant.brain.intents import match_intent
from assistant.brain.local import _small_talk
from assistant.tools import quick
from assistant.tools.registry import ToolError

ROUTES = [
    ("what time is it", "get_time"), ("what's the date today", "get_time"), ("what day is it", "get_time"),
    ("open discord", "open_app"), ("open file explorer", "open_app"), ("open youtube", "open_website"),
    ("close discord", "window"), ("minimize this", "window"), ("switch to discord", "window"),
    ("volume up", "volume"), ("set volume to 50", "volume"), ("mute", "volume"),
    ("pause", "media"), ("resume", "media"), ("skip", "media"), ("play drake", "play_music"),
    ("pause the video", "video"), ("full screen", "video"),
    ("set a timer for ten minutes", "set_timer"), ("remind me in 20 minutes to check the oven", "set_reminder"),
    ("remind me to call mum at 6 pm", "set_reminder"), ("set an alarm for 7 am", "set_alarm"),
    ("wake me up at 7:30 am", "set_alarm"), ("what timers do i have", "list_timers"),
    ("tell me when my download finishes", "watch"),
    ("what's the weather", "weather"), ("is it going to rain tomorrow", "weather"), ("my city is leeds", "set_location"),
    ("search for gaming chairs", "open_website"), ("google best gaming mouse", "open_website"),
    ("shut down the pc", "power"), ("restart the computer", "power"), ("put the pc to sleep", "power"),
    ("lock my pc", "lock_pc"), ("how's my pc doing", "system_status"), ("what's my cpu temperature", "system_status"),
    ("how much battery do i have", "system_status"), ("open my downloads folder", "open_file"),
    ("what's 15 times 23", "calculate"), ("what is the square root of 144", "calculate"),
    ("remember that my dog is called max", "remember"), ("show the grid", "mouse_grid"), ("type hello world", "type_text"),
]


@pytest.mark.parametrize("text,tool", ROUTES)
def test_everyday_commands_are_done_directly(text, tool):
    got = match_intent(text)
    assert got is not None and got[0] == tool, got


@pytest.mark.parametrize("text", ["what is the capital of france", "tell me a joke", "how do i make pancakes",
                                  "restart", "look up the price of a ps5"])
def test_questions_still_go_to_the_model(text):
    assert match_intent(text) is None


def test_reminder_details():
    assert match_intent("remind me in ten minutes to take the pizza out") == \
        ("set_reminder", {"text": "take the pizza out", "minutes": 10.0})
    assert match_intent("remind me to call mum at 6 pm") == ("set_reminder", {"text": "call mum", "at": "6 pm"})
    assert match_intent("open my downloads folder") == ("open_file", {"path": "~/Downloads"})


@pytest.mark.parametrize("expr,answer", [("15 times 23", "That's 345."), ("20 percent of 85", "That's 17."),
                                         ("the square root of 144", "That's 12."), ("ninety times ninety", "That's 8,100."),
                                         ("7 divided by 3", "That's 2.3333.")])
def test_sums_are_exact(expr, answer):
    assert quick.calculate({"expression": expr}, None) == answer


def test_sums_are_safe():
    for bad in ("__import__('os')", "2 ** 999999", "open file"):
        with pytest.raises(ToolError):
            quick.calculate({"expression": bad}, None)
    with pytest.raises(ToolError, match="divide by zero"):
        quick.calculate({"expression": "5 divided by 0"}, None)


def fake_weather():
    def handler(req: httpx.Request):
        if "geocoding" in req.url.host:
            return httpx.Response(200, json={"results": [{"name": "Leeds", "latitude": 53.8, "longitude": -1.55}]})
        return httpx.Response(200, json={"current": {"temperature_2m": 14.4, "weather_code": 3, "apparent_temperature": 12},
                                         "daily": {"temperature_2m_max": [16.2, 18], "temperature_2m_min": [9, 10],
                                                   "precipitation_probability_max": [40, 5], "weather_code": [61, 1]}})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_weather_for_the_owners_city(ctx):
    with pytest.raises(ToolError, match="my city is"):
        await quick.weather({}, ctx, _http=fake_weather())
    quick.set_location({"city": "leeds"}, ctx)
    assert await quick.weather({}, ctx, _http=fake_weather()) == \
        "It's 14 degrees and cloudy in Leeds, with a high of 16, 40 percent chance of rain."
    assert await quick.weather({"tomorrow": True}, ctx, _http=fake_weather()) == \
        "Tomorrow in Leeds: mostly clear, 10 to 18 degrees."


def test_small_talk_is_instant():
    assert _small_talk("Thanks Nova!", ", sir") == "You're welcome, sir."
    assert _small_talk("good morning nova", ", sir") == "Good morning, sir."
    assert _small_talk("thanks, open steam", ", sir") is None          # a request: not small talk
