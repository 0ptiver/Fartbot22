"""Quick answers the small local model gets wrong or slowly: sums and the weather.

- calculate: "what's 15 times 23", "20 percent of 85", "144 divided by 12". Worked out exactly
  (a tiny safe arithmetic evaluator, no eval).
- weather: Open-Meteo (free, no key, no account). Your city is set once ("my city is Leeds")
  and kept in data/location.json; only the city's coordinates are sent to Open-Meteo.
"""

from __future__ import annotations

import ast
import json
import math
import operator
import re

from assistant.core.config import ROOT
from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry

LOCATION_FILE = ROOT / "data" / "location.json"

# --- calculate -------------------------------------------------------------------------------
_WORDS = [(r"\bmultiplied by\b|\btimes\b|×|\bx\b", "*"), (r"\bdivided by\b|\bover\b|÷", "/"),
          (r"\bplus\b|\band\b", "+"), (r"\bminus\b|\btake away\b", "-"),
          (r"\bsquared\b", "**2"), (r"\bcubed\b", "**3"), (r"\bto the power of\b", "**")]
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ToolError("That number is too big to say out loud.")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "sqrt" and len(node.args) == 1:
        return math.sqrt(_eval(node.args[0]))
    raise ToolError("I can only do plain arithmetic.")


def to_expression(text: str) -> str:
    from assistant.voice.textnorm import normalize_words
    t = " ".join(normalize_words(text)) if re.search(r"[a-z]", text.lower()) else text
    t = t.lower().replace(",", "")
    t = re.sub(r"^(?:the|a)\s+", "", t.strip())
    t = re.sub(r"square root of (\d+(?:\.\d+)?)", r"sqrt(\1)", t)
    t = re.sub(r"(\d+(?:\.\d+)?) ?(?:percent|%) of (\d+(?:\.\d+)?)", r"(\1/100*\2)", t)
    for pat, op in _WORDS:
        t = re.sub(pat, f" {op} ", t)
    t = re.sub(r"\bpercent\b|%", "/100", t)
    return re.sub(r"\s+", " ", t).strip()


def spoken_number(x: float) -> str:
    if isinstance(x, float) and x.is_integer():
        x = int(x)
    if isinstance(x, int):
        return f"{x:,}"
    return f"{x:,.4f}".rstrip("0").rstrip(".")


def calculate(args: dict, ctx: ToolContext) -> str:
    expr = to_expression(args["expression"])
    if not re.fullmatch(r"[\d\s.+\-*/()%sqrt]+", expr):
        raise ToolError("I can only do plain arithmetic.")
    try:
        value = _eval(ast.parse(expr, mode="eval"))
    except ZeroDivisionError as e:
        raise ToolError("You can't divide by zero.") from e
    except (SyntaxError, TypeError, ValueError) as e:
        raise ToolError("I couldn't work that sum out.") from e
    return f"That's {spoken_number(value)}."


_CALC = re.compile(r"^(?:what(?:'?s| is)|how much is|calculate|work out)\s+(?P<e>(?=.*\d).+?)\??$")


def calc_intent(t: str) -> tuple[str, dict] | None:
    m = _CALC.match(t)
    if not m:
        return None
    expr = to_expression(m.group("e"))
    if re.fullmatch(r"[\d\s.+\-*/()%sqrt]+", expr) and re.search(r"\d\s*[-+*/]|\*\*|sqrt|/100", expr):
        return "calculate", {"expression": m.group("e")}
    return None


# --- weather ---------------------------------------------------------------------------------
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy", 45: "foggy", 48: "foggy",
       51: "drizzly", 53: "drizzly", 55: "drizzly", 61: "rainy", 63: "rainy", 65: "pouring", 66: "freezing rain",
       67: "freezing rain", 71: "snowing", 73: "snowing", 75: "snowing heavily", 77: "snowing", 80: "showery",
       81: "showery", 82: "heavy showers", 85: "snow showers", 86: "snow showers", 95: "stormy", 96: "stormy", 99: "stormy"}


def saved_city() -> str | None:
    try:
        return json.loads(LOCATION_FILE.read_text(encoding="utf-8")).get("city") or None
    except (OSError, ValueError):
        return None


def set_location(args: dict, ctx: ToolContext) -> str:
    city = args["city"].strip().strip(".").title()
    LOCATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCATION_FILE.write_text(json.dumps({"city": city}), encoding="utf-8")
    return f"Got it, you're in {city}. I'll use that for the weather."


async def weather(args: dict, ctx: ToolContext, _http=None) -> str:
    import httpx
    city = (args.get("place") or "").strip() or saved_city()
    if not city:
        raise ToolError("Which city are you in? Say “my city is …” and I'll remember it.")
    tomorrow = bool(args.get("tomorrow"))
    http = _http or httpx.AsyncClient(timeout=8)
    try:
        g = (await http.get("https://geocoding-api.open-meteo.com/v1/search",
                            params={"name": city, "count": 1, "language": "en"})).json()
        if not g.get("results"):
            raise ToolError(f"I can't find a place called {city}.")
        place = g["results"][0]
        f = (await http.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
            "current": "temperature_2m,weather_code,apparent_temperature",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "forecast_days": 2})).json()
    except httpx.HTTPError as e:
        raise ToolError("I couldn't reach the weather service.") from e
    finally:
        if _http is None:
            await http.aclose()
    d = f["daily"]
    i = 1 if tomorrow else 0
    hi, lo, rain = round(d["temperature_2m_max"][i]), round(d["temperature_2m_min"][i]), d["precipitation_probability_max"][i]
    rain_txt = f", {rain} percent chance of rain" if rain is not None and rain >= 20 else ""
    if tomorrow:
        return (f"Tomorrow in {place['name']}: {WMO.get(d['weather_code'][1], 'mixed')}, "
                f"{lo} to {hi} degrees{rain_txt}.")
    c = f["current"]
    return (f"It's {round(c['temperature_2m'])} degrees and {WMO.get(c['weather_code'], 'mixed')} in "
            f"{place['name']}, with a high of {hi}{rain_txt}.")


_WEATHER = re.compile(r"^(?:what(?:'?s| is) the |how(?:'?s| is) the |what(?:'?s| is) )?(?:weather|forecast)"
                      r"(?: (?:like|going to be like|gonna be like|looking like))?"
                      r"(?: (?:today|now|outside|right now|(?P<tomorrow>tomorrow)))?(?: in (?P<place>[a-z .'-]+?))?"
                      r"(?: (?P<tomorrow2>tomorrow)| today)?$"
                      r"|^(?:is it|will it) (?:going to |gonna )?(?:rain|snow)(?:ing)?(?: (?P<tomorrow3>tomorrow)| today)?$"
                      r"|^do i need (?:a coat|an umbrella)(?: today| (?P<tomorrow4>tomorrow))?$")
_CITY = re.compile(r"^(?:my city is|i live in|i'?m in|i am in|my location is|set my (?:city|location) to) "
                   r"(?P<city>[a-z .'-]{2,40})$")


def quick_intent(t: str) -> tuple[str, dict] | None:
    if m := _CITY.match(t):
        return "set_location", {"city": m.group("city")}
    if m := _WEATHER.match(t):
        args = {}
        if any(m.group(g) for g in ("tomorrow", "tomorrow2", "tomorrow3", "tomorrow4")):
            args["tomorrow"] = True
        if m.group("place"):
            args["place"] = m.group("place")
        return "weather", args
    return calc_intent(t)


def register(reg: ToolRegistry) -> None:
    reg.tool("calculate", "Exact arithmetic: '15 times 23', '20 percent of 85', 'square root of 144'.",
             {"type": "object", "properties": {"expression": {"type": "string", "minLength": 1, "maxLength": 200}},
              "required": ["expression"], "additionalProperties": False}, risk=Risk.SAFE, category="system")(calculate)
    reg.tool("weather", "Current weather or tomorrow's forecast for the user's city (or place).",
             {"type": "object", "properties": {"place": {"type": "string", "maxLength": 80},
                                               "tomorrow": {"type": "boolean"}},
              "additionalProperties": False}, risk=Risk.SAFE, category="web")(weather)
    reg.tool("set_location", "Remember which city the user lives in (for the weather).",
             {"type": "object", "properties": {"city": {"type": "string", "minLength": 2, "maxLength": 60}},
              "required": ["city"], "additionalProperties": False}, risk=Risk.SAFE, category="system")(set_location)
