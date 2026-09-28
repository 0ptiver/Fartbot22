"""Fact-check a web address before Nova opens it. Owner: "you need to make it fact check what the
real url is before it goes to it, it just opened this scam website like 50 times" (kbbb.com, a
lookalike of kbb.com, Kelley Blue Book; one letter off, easy to mistype or mishear).

- A lookalike of a site Nova knows (one letter off, a digit for a letter, the brand's name with a
  different ending): the real site is opened instead, and Nova says so.
- Sites the owner banned ("never take me to that website again"): never opened, by any tool.
- Everything else goes as asked.
Only the address is looked at, on this PC; nothing is sent anywhere to check it.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from assistant.core.config import ROOT
from assistant.tools.registry import ToolError

BLOCKED_FILE = ROOT / "data" / "blocked-sites.json"

# Official sites people ask for (plus Nova's browser NAMES and pc.SITES, added at lookup).
POPULAR = {
    "google.com": "Google", "youtube.com": "YouTube", "facebook.com": "Facebook", "instagram.com": "Instagram",
    "twitter.com": "Twitter", "x.com": "X", "reddit.com": "Reddit", "amazon.com": "Amazon", "ebay.com": "eBay",
    "wikipedia.org": "Wikipedia", "netflix.com": "Netflix", "twitch.tv": "Twitch", "tiktok.com": "TikTok",
    "spotify.com": "Spotify", "discord.com": "Discord", "steampowered.com": "Steam", "steamcommunity.com": "Steam",
    "epicgames.com": "Epic Games", "rockstargames.com": "Rockstar Games", "fivem.net": "FiveM", "paypal.com": "PayPal",
    "microsoft.com": "Microsoft", "apple.com": "Apple", "outlook.com": "Outlook", "live.com": "Microsoft",
    "gmail.com": "Gmail", "yahoo.com": "Yahoo", "bing.com": "Bing", "duckduckgo.com": "DuckDuckGo",
    "github.com": "GitHub", "linkedin.com": "LinkedIn", "chatgpt.com": "ChatGPT", "openai.com": "OpenAI",
    "claude.ai": "Claude", "anthropic.com": "Anthropic", "kbb.com": "Kelley Blue Book", "edmunds.com": "Edmunds",
    "carfax.com": "Carfax", "carvana.com": "Carvana", "cars.com": "Cars.com", "autotrader.com": "Autotrader",
    "carmax.com": "CarMax", "craigslist.org": "Craigslist", "zillow.com": "Zillow", "walmart.com": "Walmart",
    "target.com": "Target", "bestbuy.com": "Best Buy", "costco.com": "Costco", "homedepot.com": "Home Depot",
    "imdb.com": "IMDb", "espn.com": "ESPN", "bbc.co.uk": "BBC", "bbc.com": "BBC", "cnn.com": "CNN",
    "nytimes.com": "The New York Times", "weather.com": "The Weather Channel", "chase.com": "Chase",
    "bankofamerica.com": "Bank of America", "wellsfargo.com": "Wells Fargo", "capitalone.com": "Capital One",
    "venmo.com": "Venmo", "cash.app": "Cash App", "coinbase.com": "Coinbase", "roblox.com": "Roblox",
    "minecraft.net": "Minecraft", "nvidia.com": "NVIDIA", "hulu.com": "Hulu", "disneyplus.com": "Disney+",
    "primevideo.com": "Prime Video", "max.com": "Max", "crunchyroll.com": "Crunchyroll", "pinterest.com": "Pinterest",
    "snapchat.com": "Snapchat", "whatsapp.com": "WhatsApp", "telegram.org": "Telegram", "etsy.com": "Etsy",
    "aliexpress.com": "AliExpress", "temu.com": "Temu", "shein.com": "Shein", "nike.com": "Nike",
    "ticketmaster.com": "Ticketmaster", "nbc.com": "NBC", "abc.com": "ABC", "cbs.com": "CBS", "fox.com": "Fox", "irs.gov": "IRS", "usps.com": "USPS", "ups.com": "UPS", "fedex.com": "FedEx",
}
_DIGITS = str.maketrans("0135$@", "olesa" + "a")
LAST_OPENED: dict[str, str] = {"host": ""}


def host_of(url: str) -> str:
    u = url.strip()
    if "://" not in u:
        u = "https://" + u
    return (urlparse(u).hostname or "").lower().removeprefix("www.")


def _known() -> dict[str, str]:
    known = dict(POPULAR)
    try:
        from assistant.tools.browser import NAMES
        for name, dom in NAMES.items():
            known.setdefault(host_of(dom), name.title())
    except Exception:
        pass
    try:
        from assistant.tools.pc import SITES
        for name, url in SITES.items():
            known.setdefault(host_of(url), name.title())
    except Exception:
        pass
    return known


def _split(host: str) -> tuple[str, str]:
    """'kbb.com' -> ('kbb', 'com'); 'bbc.co.uk' -> ('bbc', 'co.uk'); 'news.bbc.co.uk' -> ('bbc', 'co.uk')."""
    parts = host.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov"):
        return parts[-3], ".".join(parts[-2:])
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return host, ""


def _distance(a: str, b: str) -> int:
    """Edit distance (with swapped neighbours counting as one)."""
    prev2, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if prev2 is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def _undouble(s: str) -> str:
    return re.sub(r"(.)\1+", r"\1", s)


def lookalike(host: str) -> tuple[str, str] | None:
    """(real host, its name) when `host` imitates a site Nova knows; None when it's fine."""
    known = _known()
    if not host or host in known or any(host.endswith("." + k) for k in known):
        return None
    label, tld = _split(host)
    best = None
    for real, name in known.items():
        rlabel, rtld = _split(real)
        if label == rlabel:
            if rtld == "com" and tld in ("co", "cm", "om", "con", "corn", "comm", "cmo"):
                return real, name                # "kbb.co": the classic missing letter
            continue                            # same name, another country's site (amazon.co.uk)
        d = _distance(label, rlabel)
        swapped = label.translate(_DIGITS) == rlabel                   # "g00gle", "paypa1"
        doubled = (_undouble(label) == _undouble(rlabel) and d <= 2      # "kbbb", "gooogle", "youttube"
                   and len(label) > len(rlabel))
        # Short names are nearly all real companies (nbc, abc, ping): only the tricks above count.
        close = (d == 1 and len(rlabel) >= 5) or (d == 2 and len(rlabel) >= 8)
        close = close or doubled
        if (swapped or close) and (best is None or d < best[0]):
            best = (d, real, name)
    return (best[1], best[2]) if best else None


# --- sites the owner banned --------------------------------------------------------------------
def blocked() -> set[str]:
    try:
        return set(json.loads(BLOCKED_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def block(host: str) -> None:
    hosts = blocked() | {host}
    BLOCKED_FILE.parent.mkdir(parents=True, exist_ok=True)
    BLOCKED_FILE.write_text(json.dumps(sorted(hosts)), encoding="utf-8")


def unblock(host: str) -> bool:
    hosts = blocked()
    if host not in hosts:
        return False
    BLOCKED_FILE.write_text(json.dumps(sorted(hosts - {host})), encoding="utf-8")
    return True


# --- the check every tool uses -------------------------------------------------------------------
def check(url: str) -> tuple[str, str]:
    """The address to actually open, and what to tell the owner about it ('' when nothing).
    Raises ToolError for a banned site."""
    host = host_of(url)
    if not host:
        return url, ""
    if host in blocked() or any(host.endswith("." + b) for b in blocked()):
        raise ToolError(f"You told me never to open {host}, so I didn't.")
    real = lookalike(host)
    if real:
        real_host, name = real
        if real_host in blocked():
            raise ToolError(f"{host} looks like a fake copy of {real_host}, and you told me not to open that either.")
        rest = url.split(host, 1)[1] if host in url else ""
        fixed = "https://" + real_host + (rest if rest.startswith("/") else "")
        LAST_OPENED["host"] = real_host
        return fixed, f"{host} isn't {name}; it's a lookalike, so I opened the real {real_host} instead."
    LAST_OPENED["host"] = host
    return url, ""


def check_text(what: str) -> tuple[str, str]:
    """For a spoken destination that may or may not be an address."""
    if re.search(r"[\w-]+(?:\.[\w-]+)+", what):
        return check(what)
    return what, ""


def block_site(args: dict, ctx) -> str:
    action = args.get("action", "list")
    site = (args.get("site") or "").strip()
    if action == "list":
        hosts = sorted(blocked())
        return ("Blocked: " + ", ".join(hosts) + ".") if hosts else "No sites are blocked."
    host = host_of(site) if "." in site else ""
    if action == "block":
        host = host or LAST_OPENED.get("host", "")
        if not host:
            raise ToolError("Which website?")
        block(host)
        return f"Blocked {host}. I won't open it again."
    if not host:
        raise ToolError("Which website? Say it with its ending, like kbb.com.")
    return f"Unblocked {host}." if unblock(host) else f"{host} wasn't blocked."


def register(reg) -> None:
    from assistant.tools.registry import Risk
    reg.tool("block_site", "Block or unblock a website so Nova never opens it, or list blocked sites.",
             {"type": "object", "properties": {"action": {"type": "string", "enum": ["block", "unblock", "list"]},
                                               "site": {"type": "string", "maxLength": 200}},
              "required": ["action"], "additionalProperties": False}, risk=Risk.SAFE, category="web")(block_site)


_BLOCK = re.compile(r"^(?:(?:block|ban|blacklist) (?:the )?(?:site |website )?(?P<b>\S+\.\S+|that (?:site|website|page))"
                    r"|never (?:open|go to|take me to) (?P<n>\S+\.\S+)(?: again)?"
                    r"|(?:unblock|allow|unban) (?:the )?(?:site |website )?(?P<u>\S+\.\S+)"
                    r"|(?:what|which) (?:sites|websites) (?:are|have you) blocked|(?:list|show)(?: me)? (?:the )?blocked (?:sites|websites))$")


def block_intent(t: str) -> tuple[str, dict] | None:
    m = _BLOCK.match(t)
    if not m:
        return None
    if m.group("u"):
        return "block_site", {"action": "unblock", "site": m.group("u")}
    site = m.group("b") or m.group("n")
    if site:
        return "block_site", {"action": "block", **({} if site.startswith("that ") else {"site": site})}
    return "block_site", {"action": "list"}
