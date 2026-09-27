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


# --- conversions ------------------------------------------------------------------------------
# unit -> (kind, factor to the base unit)
_UNITS = {}
for kind, table in {
    "length": {("mm", "millimetre", "millimeter"): 0.001, ("cm", "centimetre", "centimeter"): 0.01,
               ("m", "metre", "meter"): 1, ("km", "kilometre", "kilometer", "k"): 1000,
               ("in", "inch", "inche"): 0.0254, ("ft", "foot", "feet"): 0.3048, ("yd", "yard"): 0.9144,
               ("mi", "mile"): 1609.344},
    "weight": {("g", "gram", "gramme"): 0.001, ("kg", "kilo", "kilogram", "kilogramme"): 1,
               ("oz", "ounce"): 0.028349523125, ("lb", "lbs", "pound"): 0.45359237, ("st", "stone"): 6.35029318,
               ("ton", "tonne"): 1000},
    "volume": {("ml", "millilitre", "milliliter"): 0.001, ("l", "litre", "liter"): 1, ("cup",): 0.2365882365,
               ("pint",): 0.473176473, ("gallon",): 3.785411784, ("fl oz", "fluid ounce"): 0.0295735295625},
    "speed": {("mph", "miles per hour", "mile per hour"): 0.44704, ("kph", "km/h", "kmh", "kilometres per hour",
              "kilometers per hour", "kilometre per hour", "kilometer per hour"): 1 / 3.6,
              ("m/s", "metres per second", "meters per second"): 1},
}.items():
    for names, factor in table.items():
        for n in names:
            _UNITS[n] = (kind, factor, next((x for x in names if len(x) > 3 and "/" not in x and x != "lbs"), names[0]))
_TEMPS = {"c": "c", "celsius": "c", "centigrade": "c", "degrees c": "c", "degrees celsius": "c",
          "f": "f", "fahrenheit": "f", "degrees f": "f", "degrees fahrenheit": "f", "degrees": None}
_CURRENCY = {"dollar": "USD", "dollars": "USD", "usd": "USD", "bucks": "USD", "us dollars": "USD",
             "pound": "GBP", "pounds": "GBP", "quid": "GBP", "gbp": "GBP", "pounds sterling": "GBP",
             "euro": "EUR", "euros": "EUR", "eur": "EUR", "yen": "JPY", "canadian dollars": "CAD",
             "canadian dollar": "CAD", "australian dollars": "AUD", "australian dollar": "AUD", "pesos": "MXN",
             "peso": "MXN", "rupees": "INR", "rupee": "INR", "won": "KRW", "yuan": "CNY", "francs": "CHF",
             "swiss francs": "CHF", "krona": "SEK", "kronor": "SEK", "zloty": "PLN", "rand": "ZAR"}
_CUR_NAMES = {"USD": "dollars", "GBP": "pounds", "EUR": "euros", "JPY": "yen", "CAD": "Canadian dollars",
              "AUD": "Australian dollars", "MXN": "pesos", "INR": "rupees", "KRW": "won", "CNY": "yuan",
              "CHF": "Swiss francs", "SEK": "kronor", "PLN": "zloty", "ZAR": "rand"}


def _unit(word: str):
    w = word.strip().lower().rstrip(".")
    for cand in (w, w[:-1] if w.endswith("s") else w, w[:-2] if w.endswith("es") else w):
        if cand in _UNITS:
            return _UNITS[cand]
    return None


def _plural(name: str, value: float) -> str:
    if abs(value) == 1 or name.endswith("s") or "/" in name or " per " in name or name in ("mph", "kph", "stone"):
        return name.replace("miles per", "mile per") if abs(value) == 1 else name.replace("mile per", "miles per")
    return {"foot": "feet", "inch": "inches"}.get(name, name + "s")


async def convert(args: dict, ctx: ToolContext, _http=None) -> str:
    amount, frm, to = float(args["amount"]), args["from"].strip().lower(), args["to"].strip().lower()
    if frm in _TEMPS and to in _TEMPS:
        a, b = _TEMPS[frm] or ("f" if _TEMPS[to] == "c" else "c"), _TEMPS[to] or ("f" if _TEMPS[frm] == "c" else "c")
        value = (amount - 32) * 5 / 9 if (a, b) == ("f", "c") else amount * 9 / 5 + 32 if (a, b) == ("c", "f") else amount
        return f"{spoken_number(round(amount, 2))} degrees {a.upper()} is {spoken_number(round(value, 1))} degrees {b.upper()}."
    if frm in _CURRENCY and to in _CURRENCY:
        import httpx
        http = _http or httpx.AsyncClient(timeout=8, follow_redirects=True)
        a, b = _CURRENCY[frm], _CURRENCY[to]
        value = None
        try:
            # Frankfurter: the European Central Bank's daily rates, free, no key or account.
            for url in ("https://api.frankfurter.dev/v1/latest", "https://api.frankfurter.app/latest"):
                try:
                    value = (await http.get(url, params={"amount": amount, "from": a, "to": b})).json()["rates"][b]
                    break
                except (httpx.HTTPError, KeyError, ValueError):
                    continue
        finally:
            if _http is None:
                await http.aclose()
        if value is None:
            raise ToolError("I couldn't get today's exchange rate.")
        return (f"{spoken_number(round(amount, 2))} {_CUR_NAMES.get(a, a)} is about "
                f"{spoken_number(round(value, 2))} {_CUR_NAMES.get(b, b)}.")
    u1, u2 = _unit(frm), _unit(to)
    if not u1 or not u2:
        raise ToolError(f"I don't know how to convert {frm} to {to}.")
    if u1[0] != u2[0]:
        raise ToolError(f"You can't turn {u1[0]} into {u2[0]}.")
    value = amount * u1[1] / u2[1]
    shown = round(value, 2) if abs(value) >= 1 else round(value, 4)
    return f"{spoken_number(round(amount, 4))} {_plural(u1[2], amount)} is {spoken_number(shown)} {_plural(u2[2], value)}."


_NUM = r"(\d+(?:\.\d+)?|a|an|one)"
_CONVERT = re.compile(r"^(?:convert |change |turn )?" + _NUM + r" ([a-z/ ]{1,25}?) (?:to|into|in|as) ([a-z/ ]{1,25}?)$"
                      r"|^(?:what(?:'?s| is|'re| are) |how much (?:is|are) )" + _NUM + r" ([a-z/ ]{1,25}?) (?:in|to) ([a-z/ ]{1,25}?)$"
                      r"|^how many ([a-z/ ]{1,25}?) (?:are )?(?:in|is|are|make) " + _NUM + r" ([a-z/ ]{1,25}?)$")


def convert_intent(t: str) -> tuple[str, dict] | None:
    from assistant.voice.textnorm import normalize_words
    t = re.sub(r"\$\s?(\d+(?:\.\d+)?)", r"\1 dollars", t)
    t = re.sub(r"£\s?(\d+(?:\.\d+)?)", r"\1 pounds", t)
    t = re.sub(r"€\s?(\d+(?:\.\d+)?)", r"\1 euros", t)
    n = " ".join(normalize_words(t)).replace("degrees celsius", "celsius").replace("degrees fahrenheit", "fahrenheit")
    m = _CONVERT.match(n)
    if not m:
        return None
    g = m.groups()
    if g[0]:
        amount, frm, to = g[0], g[1], g[2]
    elif g[3]:
        amount, frm, to = g[3], g[4], g[5]
    else:
        to, amount, frm = g[6], g[7], g[8]
    amount = 1.0 if amount in ("a", "an", "one") else float(amount)
    frm, to = frm.strip(), to.strip()
    known = lambda u: u in _TEMPS or u in _CURRENCY or _unit(u) is not None   # noqa: E731
    if not (known(frm) and known(to)):
        return None
    return "convert", {"amount": amount, "from": frm, "to": to}


# --- days until -------------------------------------------------------------------------------
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december"]


def _easter(year: int):
    from datetime import date
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l_) // 433
    month = (h + l_ - 7 * m + 90) // 25
    return date(year, month, (h + l_ - 7 * m + 33 * month + 19) % 32)


_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
        "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
        "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20, "thirtieth": 30}


def _plain_date(text: str) -> str:
    """'the twenty fifth of december' / 'the 25th of december' -> 'the 25 of december'."""
    t = re.sub(r"(\d+)\s*(?:st|nd|rd|th)\b", r"\1", text)
    t = re.sub(r"\b(twenty|thirty)[ -](first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)\b",
               lambda m: str({"twenty": 20, "thirty": 30}[m.group(1)] + _ORD[m.group(2)]), t)
    return re.sub(r"\b(" + "|".join(_ORD) + r")\b", lambda m: str(_ORD[m.group(1)]), t)


def _occasion(name: str, year: int):
    name = _plain_date(name)
    from datetime import date
    fixed = {"christmas": (12, 25), "christmas day": (12, 25), "christmas eve": (12, 24), "boxing day": (12, 26),
             "new year": (1, 1), "new years": (1, 1), "new year's": (1, 1), "new year's day": (1, 1),
             "new years day": (1, 1), "new year's eve": (12, 31), "new years eve": (12, 31), "halloween": (10, 31),
             "valentine's day": (2, 14), "valentines day": (2, 14), "valentines": (2, 14), "valentine's": (2, 14),
             "bonfire night": (11, 5), "guy fawkes night": (11, 5), "st patrick's day": (3, 17),
             "independence day": (7, 4), "the fourth of july": (7, 4), "fourth of july": (7, 4)}
    if name in fixed:
        return date(year, *fixed[name])
    if name in ("easter", "easter sunday"):
        return _easter(year)
    m = (re.fullmatch(r"(?:the )?(\d{1,2}) (?:of )?(" + "|".join(_MONTHS) + ")", name)
         or re.fullmatch(r"(" + "|".join(_MONTHS) + r") (?:the )?(\d{1,2})", name))
    if m:
        a, b = m.groups()
        day, month = (int(a), b) if a.isdigit() else (int(b), a)
        try:
            return date(year, _MONTHS.index(month) + 1, day)
        except ValueError:
            return None
    return None


def days_until(args: dict, ctx: ToolContext) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    what = args["what"].lower().strip(" ?.!")
    tz = ctx.settings.assistant.timezone if ctx else "UTC"
    today = datetime.now(ZoneInfo(tz)).date()
    when = _occasion(what, today.year)
    if when is None:
        raise ToolError(f"I don't know when {args['what']} is.")
    if when < today:
        when = _occasion(what, today.year + 1)
    days = (when - today).days
    is_date = bool(re.search(r"\d", _plain_date(what)))
    nice = f"{when:%A} {when.day} {when:%B}" if is_date else args["what"].strip(" ?.!").title().replace("'S", "'s")
    if days == 0:
        return f"{nice} is today."
    if days == 1:
        return f"{nice} is tomorrow."
    weeks = f", about {round(days / 7)} weeks" if days >= 21 else ""
    return f"{days} days until {nice}{weeks}." + ("" if is_date else f" It's on a {when:%A}.")


_UNTIL = re.compile(r"^(?:how (?:many|much) (?:days|time) (?:is (?:it |there )?|are there |have i got |do i have |left )?"
                    r"(?:left )?(?:until|till|til|to|before)|how long (?:is it |until|till|til|to|before)(?: until| till)?"
                    r"|when is|what day is) (?P<what>.+?)$")


def until_intent(t: str) -> tuple[str, dict] | None:
    from datetime import date
    m = _UNTIL.match(t)
    if m and _occasion(m.group("what"), date.today().year):
        return "days_until", {"what": m.group("what")}
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
    return until_intent(t) or convert_intent(t) or calc_intent(t)


def register(reg: ToolRegistry) -> None:
    reg.tool("calculate", "Exact arithmetic: '15 times 23', '20 percent of 85', 'square root of 144'.",
             {"type": "object", "properties": {"expression": {"type": "string", "minLength": 1, "maxLength": 200}},
              "required": ["expression"], "additionalProperties": False}, risk=Risk.SAFE, category="system")(calculate)
    reg.tool("convert", "Convert units (length, weight, volume, speed, temperature) or money between currencies "
             "(today's rate): amount, from, to, e.g. 100, 'dollars', 'pounds'.",
             {"type": "object", "properties": {"amount": {"type": "number"},
                                               "from": {"type": "string", "minLength": 1, "maxLength": 30},
                                               "to": {"type": "string", "minLength": 1, "maxLength": 30}},
              "required": ["amount", "from", "to"], "additionalProperties": False}, risk=Risk.SAFE, category="web")(convert)
    reg.tool("days_until", "How many days until a date or holiday ('christmas', 'the 3rd of june').",
             {"type": "object", "properties": {"what": {"type": "string", "minLength": 2, "maxLength": 60}},
              "required": ["what"], "additionalProperties": False}, risk=Risk.SAFE, category="system")(days_until)
    reg.tool("weather", "Current weather or tomorrow's forecast for the user's city (or place).",
             {"type": "object", "properties": {"place": {"type": "string", "maxLength": 80},
                                               "tomorrow": {"type": "boolean"}},
              "additionalProperties": False}, risk=Risk.SAFE, category="web")(weather)
    reg.tool("set_location", "Remember which city the user lives in (for the weather).",
             {"type": "object", "properties": {"city": {"type": "string", "minLength": 2, "maxLength": 60}},
              "required": ["city"], "additionalProperties": False}, risk=Risk.SAFE, category="system")(set_location)
