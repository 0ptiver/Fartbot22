"""Owner: "Nova is still struggling to answer me on simple commands". The everyday things
people say are done directly (no model, no chatter); this table guards where each one goes."""

import re

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
    # second audit (owner: "continue polishing")
    ("make it louder", "volume"), ("it's too loud", "volume"), ("how much ram am i using", "system_status"),
    ("what time is it in tokyo", "get_time"), ("play mrbeast on youtube", "open_website"),
    ("search youtube for lofi", "open_website"), ("open youtube and search for lofi", "open_website"),
    ("skip the ad", "video"), ("skip ahead 30 seconds", "video"), ("rewind 10 seconds", "video"),
    ("what did i ask you to remember", "recall"), ("remind me tomorrow at 9 to call the bank", "set_reminder"),
    ("wake me up tomorrow at 7", "set_alarm"), ("how many days until christmas", "days_until"),
    ("empty the recycle bin", "empty_recycle_bin"), ("click on search", "click_element"),
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


def test_youtube_opens_results_in_the_normal_browser():
    """'Play MrBeast on YouTube' went to Spotify as a song called 'mrbeast on youtube'."""
    assert match_intent("Play MrBeast on YouTube.") == ("open_website", {"site": "youtube", "search": "mrbeast"})
    assert match_intent("youtube music") is None or match_intent("youtube music")[0] != "open_website"


def test_click_on_is_not_part_of_the_name():
    assert match_intent("click on search") == ("click_element", {"name": "search"})
    assert match_intent("double click on the file") == ("click_element", {"name": "the file", "action": "double_click"})


def test_type_my_email_is_not_typed_literally():
    assert match_intent("type my email") is None                 # the model (with memories) decides
    assert match_intent("type hello there") == ("type_text", {"text": "hello there"})


def test_reminders_on_a_day():
    assert match_intent("remind me tomorrow at 9 to call the bank") == \
        ("set_reminder", {"text": "call the bank", "at": "tomorrow at 9"})
    assert match_intent("remind me to call the bank on friday at 3 pm") == \
        ("set_reminder", {"text": "call the bank", "at": "on friday at 3 pm"})
    assert match_intent("set an alarm for 7 am tomorrow") == ("set_alarm", {"at": "7 am tomorrow"})


@pytest.mark.parametrize("said,day,hour", [
    ("9", 0, 21), ("tomorrow at 9", 1, 9), ("tomorrow at 3", 1, 15), ("9 am tomorrow", 1, 9),
    ("tonight at 8", 0, 20), ("friday at 3 pm", 5, 15), ("on monday at 10", 1, 10), ("noon tomorrow", 1, 12),
])
def test_clock_with_a_day(said, day, hour):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from assistant.core.scheduler import parse_clock
    now = datetime(2026, 9, 27, 15, 0, tzinfo=ZoneInfo("Europe/London"))           # a Sunday, 3 pm
    when = parse_clock(said, "Europe/London", now)
    assert ((when.date() - now.date()).days, when.hour) == (day, hour)


def test_time_somewhere_else(local_settings):
    from assistant.tools.registry import ToolContext
    from assistant.tools.system import get_time, zone_for
    assert str(zone_for("new york")) == "America/New_York" and str(zone_for("LA")) == "America/Los_Angeles"
    out = get_time({"place": "tokyo"}, ToolContext(local_settings))
    assert out.startswith("It's ") and "in Tokyo" in out and ("ahead" in out or "behind" in out or "same" in out)
    with pytest.raises(ToolError, match="time zone"):
        get_time({"place": "atlantis"}, ToolContext(local_settings))


@pytest.mark.parametrize("what,ok", [("christmas", True), ("the 3rd of june", True), ("the twenty fifth of december", True),
                                     ("june 1st", True), ("easter", True), ("my birthday", False)])
def test_days_until(what, ok):
    assert (quick.until_intent(f"how many days until {what}") is not None) == ok
    if ok:
        out = quick.days_until({"what": what}, None)
        assert re.search(r"days until|is today|is tomorrow", out)


def test_easter_dates():
    from datetime import date
    assert quick._easter(2026) == date(2026, 4, 5) and quick._easter(2027) == date(2027, 3, 28)


class FakeBin:
    def __init__(self, items, size, sticky=0):
        self.items, self.bytes, self.sticky, self.emptied = items, size, sticky, 0

    def size(self):
        return self.items, self.bytes

    def empty(self):
        self.emptied += 1
        self.items = self.sticky


def test_empty_recycle_bin_is_checked(monkeypatch):
    from assistant.tools import files
    monkeypatch.setattr(files, "BIN", FakeBin(12, 3 * 1024 ** 3))
    assert files.empty_recycle_bin({}, None) == "Emptied the Recycle Bin: 12 items, 3 GB freed."
    assert files.empty_recycle_bin({}, None) == "The Recycle Bin is already empty."
    monkeypatch.setattr(files, "BIN", FakeBin(3, 100, sticky=1))
    with pytest.raises(ToolError, match="1 item is still there"):
        files.empty_recycle_bin({}, None)


def test_empty_recycle_bin_asks_first_and_not_from_a_phone(registry):
    registry.settings.phone.pc_control = "off"          # the "phone can't control the PC" setting
    assert registry.effective_risk("empty_recycle_bin", remote=False).value == "confirm"
    assert registry.effective_risk("empty_recycle_bin", remote=True).value == "blocked"


@pytest.mark.parametrize("said,start", [("who are you", "I'm Nova, sir, your assistant. Oliver made me."),
                                        ("what's your name", "I'm Nova"), ("what can you do", "Quite a lot, sir.")])
def test_who_and_what(said, start):
    assert _small_talk(said, ", sir", "Oliver", "Nova").startswith(start)


@pytest.mark.parametrize("said,expected", [
    ("what's 5 miles in km", "5 miles is 8.05 kilometres."), ("how many cm in an inch", "1 inch is 2.54 centimetres."),
    ("100 f to c", "100 degrees F is 37.8 degrees C."), ("what is 70 kg in pounds", "70 kilos is 154.32 pounds."),
    ("60 mph in kph", "60 miles per hour is 96.56 kilometres per hour."), ("what is 5 stone in kg", "5 stone is 31.75 kilos."),
])
async def test_unit_conversions(said, expected):
    intent = match_intent(said)
    assert intent[0] == "convert"
    assert await quick.convert(intent[1], None) == expected


async def test_currency_uses_todays_rate():
    assert match_intent("convert 100 dollars to pounds") == ("convert", {"amount": 100.0, "from": "dollars", "to": "pounds"})
    assert match_intent("what's $50 in euros")[1] == {"amount": 50.0, "from": "dollars", "to": "euros"}
    seen = []

    def handler(req):
        seen.append(req.url)
        return httpx.Response(200, json={"amount": 100, "base": "USD", "rates": {"GBP": 74.52}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        out = await quick.convert({"amount": 100, "from": "dollars", "to": "pounds"}, None, _http=http)
    assert out == "100 dollars is about 74.52 pounds." and seen[0].params["from"] == "USD"
    with pytest.raises(ToolError, match="kilos into length|weight into length"):
        await quick.convert({"amount": 1, "from": "kg", "to": "km"}, None)


async def test_no_rate_says_so():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as http:
        with pytest.raises(ToolError, match="exchange rate"):
            await quick.convert({"amount": 1, "from": "euros", "to": "yen"}, None, _http=http)


@pytest.mark.parametrize("said,args", [("brightness up", {"action": "up"}), ("dim the screen", {"action": "down"}),
                                       ("set brightness to 70", {"action": "set", "level": 70})])
def test_brightness_phrases(said, args):
    assert match_intent(said) == ("brightness", args)


class FakeBrightness:
    def __init__(self, level, works=True):
        self.level, self.works = level, works

    def get(self):
        return self.level

    def set(self, level):
        if self.works:
            self.level = level


def test_brightness_is_checked(monkeypatch):
    from assistant.tools import system
    monkeypatch.setattr(system, "BRIGHTNESS", FakeBrightness(50))
    assert system.brightness({"action": "up"}, None) == "Brightness 70 percent."
    assert system.brightness({"action": "set", "level": 100}, None) == "Brightness 100 percent."
    monkeypatch.setattr(system, "BRIGHTNESS", FakeBrightness(50, works=False))
    with pytest.raises(ToolError, match="still 50"):
        system.brightness({"action": "down"}, None)
    monkeypatch.setattr(system, "BRIGHTNESS", FakeBrightness(None))
    with pytest.raises(ToolError, match="laptop's own screen"):
        system.brightness({"action": "up"}, None)


@pytest.mark.parametrize("said,focus", [("what's on my screen", "what's on my screen"),
                                        ("what does this error say", "what does this error say")])
def test_screen_questions_go_straight_to_the_screen(said, focus):
    assert match_intent(said) == ("look_at_screen", {"focus": focus})
