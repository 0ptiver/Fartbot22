"""Nova's own browser: it can open sites, search them, click, type, pick from dropdowns and read
pages by itself ("open kbb.com and search for a 2019 Honda Civic").

It drives a separate Microsoft Edge window (every Windows 11 PC has Edge) through Playwright,
with its own profile in data/nova-browser: your normal browser, its logins and cookies are
never touched, and Nova doesn't need the mouse, the keyboard or the window in front.

After each step the tool returns the page's title and a short numbered list of what can be
clicked or typed into, so the model can take the next step by number. Page text is
information, never instructions (the system prompt says so too).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import quote_plus, urlparse

from assistant.core.config import ROOT
from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry

log = logging.getLogger(__name__)

PROFILE_DIR = ROOT / "data" / "nova-browser"
ACTIVE_S = 15 * 60           # "search for X" goes to Nova's browser if it was used this recently
MAX_ELEMENTS = 30

NAMES = {"kelley blue book": "kbb.com", "kelly blue book": "kbb.com", "kbb": "kbb.com", "youtube": "youtube.com",
         "google": "google.com", "amazon": "amazon.com", "ebay": "ebay.com", "reddit": "reddit.com",
         "wikipedia": "wikipedia.org", "gmail": "mail.google.com", "twitch": "twitch.tv", "netflix": "netflix.com",
         "github": "github.com", "facebook": "facebook.com", "instagram": "instagram.com", "twitter": "x.com",
         "x": "x.com", "tiktok": "tiktok.com", "maps": "maps.google.com", "google maps": "maps.google.com",
         "autotrader": "autotrader.com", "carmax": "carmax.com", "cars.com": "cars.com", "craigslist": "craigslist.org",
         "facebook marketplace": "facebook.com/marketplace", "steam": "store.steampowered.com", "chatgpt": "chatgpt.com",
         "claude": "claude.ai", "spotify": "open.spotify.com", "walmart": "walmart.com", "best buy": "bestbuy.com",
         "target": "target.com", "imdb": "imdb.com", "weather": "weather.com", "espn": "espn.com", "bbc": "bbc.co.uk"}

# Every visible thing on the page you can click or type into, numbered (data-nova-id).
_SNAPSHOT_JS = r"""
(max) => {
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link],[role=searchbox],' +
              '[role=textbox],[role=combobox],[role=option],[role=tab],[role=menuitem],[role=checkbox],[contenteditable=true]';
  document.querySelectorAll('[data-nova-id]').forEach(e => e.removeAttribute('data-nova-id'));
  const out = []; let n = 0;
  for (const e of document.querySelectorAll(sel)) {
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.top > innerHeight * 2) continue;
    const st = getComputedStyle(e);
    if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity) === 0) continue;
    const tag = e.tagName.toLowerCase();
    let label = e.getAttribute('aria-label') || (e.labels && e.labels[0] && e.labels[0].innerText) ||
                e.innerText || e.getAttribute('placeholder') || e.getAttribute('title') || e.getAttribute('alt') ||
                (tag === 'input' ? e.value : '') || e.getAttribute('name') || '';
    label = label.replace(/\s+/g, ' ').trim().slice(0, 70);
    if (!label && !['input', 'select', 'textarea'].includes(tag)) continue;
    n += 1;
    e.setAttribute('data-nova-id', String(n));
    const item = {n, tag, role: e.getAttribute('role') || '', type: e.getAttribute('type') || '', label};
    if (tag === 'select') item.options = [...e.options].map(o => o.text.trim()).filter(Boolean).slice(0, 30);
    out.push(item);
    if (out.length >= max) break;
  }
  return out;
}
"""

_SEARCH_BOXES = ["input[type=search]", "[role=searchbox]", "input[name=q]", "input[name*=search i]",
                 "input[placeholder*=search i]", "input[aria-label*=search i]", "input[id*=search i]",
                 "input[class*=search i]", "textarea[name=q]"]


def site_search_url(site: str, query: str) -> str:
    """The site's best page for the query: DuckDuckGo's "!ducky" jumps to the top result."""
    return "https://duckduckgo.com/?q=" + quote_plus(f"!ducky site:{site} {query}")


def resolve(site: str) -> str:
    """'kelley blue book' -> https://kbb.com, 'kbb.com/cars' -> https://kbb.com/cars; an unknown
    name goes to the first search result for it."""
    s = site.strip().strip("\"'“”").rstrip(".")
    if " " in s and (dom := re.search(r"\b[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?:/\S*)?", s, re.I)):
        s = dom.group(0)                                          # "kelley blue book kbb.com" -> kbb.com
    low = s.lower().removeprefix("the ").removesuffix(" website").removesuffix(" site").strip()
    if low in NAMES:
        s = NAMES[low]
    if "://" in s:
        url = s
    elif re.search(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", s):
        url = "https://" + s
    else:
        url = "https://duckduckgo.com/?q=" + quote_plus("!ducky " + s)      # straight to the top result
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ToolError("I only open normal web addresses (http or https).")
    return url


# Owner's case: kbb.com answered "Access Denied" because the window announced it was automated
# (navigator.webdriver, --enable-automation), and Edge showed a "--no-sandbox" warning bar.
# Nova's browser behaves like a normal Edge window instead: sandbox on, no automation banner.
_NOT_A_ROBOT_JS = "Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => undefined});"


def launch_options(executable: str | None, channel: str | None, headless: bool, windows: bool | None = None) -> dict:
    import sys
    windows = sys.platform == "win32" if windows is None else windows
    opts: dict = dict(headless=headless, no_viewport=not headless,
                      args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
                      ignore_default_args=["--enable-automation", "--no-sandbox"])
    if windows:
        opts["chromium_sandbox"] = True          # Edge's normal protection (and no warning bar)
    else:
        opts["ignore_default_args"] = ["--enable-automation"]   # Linux test containers run as root
    if executable:
        opts["executable_path"] = executable
    elif channel:
        opts["channel"] = channel
    return opts


_BLOCK_WORDS = ("access denied", "you don't have permission to access", "are you a robot", "verify you are human",
                "unusual traffic", "request blocked", "403 forbidden", "pardon our interruption")


def blocked(title: str, text: str) -> bool:
    """Did the site refuse the page (a bot wall), rather than show it?"""
    t = f"{title}\n{text[:1500]}".lower()
    return any(w in t for w in _BLOCK_WORDS)


class NovaBrowser:
    """One Edge window Nova drives. Started on first use, restarted if you close it."""

    def __init__(self):
        self._pw = None
        self._ctx = None
        self.page = None
        self.last_used = 0.0
        self.title = ""                                           # of the page, after each step
        self.executable: str | None = None      # tests: a local Chromium
        self.channel: str | None = "msedge"
        self.headless = False
        self._lock = asyncio.Lock()

    def in_front(self) -> bool:
        """Is Nova's browser the window you're looking at? ('scroll down', 'go back', 'click X')"""
        if not self.active:
            return False
        try:
            from assistant.tools import pc
            w = pc.WINDOWS.active()
        except Exception:
            return False
        return bool(w and "msedge" in w.process.lower() and self.title and self.title[:30] in w.title)

    @property
    def active(self) -> bool:
        return self.page is not None and not self.page.is_closed() and time.time() - self.last_used < ACTIVE_S

    async def get_page(self):
        async with self._lock:
            if self.page is not None and not self.page.is_closed():
                return self.page
            await self._start()
            return self.page

    async def _start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise ToolError("My browser control isn't installed. Run scripts\\update.ps1.") from e
        await self.close()
        self._pw = await async_playwright().start()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        opts = launch_options(self.executable, self.channel, self.headless)
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(str(PROFILE_DIR), **opts)
        except Exception as e:
            await self.close()
            raise ToolError(f"I couldn't start my browser ({str(e).splitlines()[0][:120]}).") from e
        self._ctx.set_default_timeout(12000)
        await self._ctx.add_init_script(_NOT_A_ROBOT_JS)
        self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._ctx.on("page", self._new_tab)                      # links that open a new tab: follow them

    def _new_tab(self, page) -> None:
        self.page = page

    async def close(self) -> None:
        for thing in (self._ctx, self._pw):
            if thing is not None:
                try:
                    await (thing.close() if thing is self._ctx else thing.stop())
                except Exception:
                    pass
        self._ctx = self._pw = self.page = None


BROWSER = NovaBrowser()


# --- page helpers ----------------------------------------------------------------------------
async def _settle(page, ms: int = 4000) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=ms)
        await page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:
        pass                                                     # busy sites never go idle: fine


async def snapshot(page) -> list[dict]:
    try:
        return await page.evaluate(_SNAPSHOT_JS, MAX_ELEMENTS)
    except Exception:
        return []


def describe_elements(items: list[dict]) -> str:
    lines = []
    for e in items:
        kind = {"a": "link", "select": "dropdown", "textarea": "text box"}.get(e["tag"]) or e["role"] or \
            ("text box" if e["tag"] == "input" and e["type"] in ("", "text", "search", "email", "tel", "number") else
             e["type"] or e["tag"])
        line = f"[{e['n']}] {kind} \"{e['label']}\""
        if e.get("options"):
            line += " options: " + ", ".join(e["options"][:12]) + (" ..." if len(e["options"]) > 12 else "")
        lines.append(line)
    return "\n".join(lines)


class Blocked(ToolError):
    pass


async def page_report(page, details: bool) -> str:
    title = (await page.title() or "").strip()[:100]
    BROWSER.title = title
    try:
        text = await page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 1500)")
    except Exception:
        text = ""
    if blocked(title, text):
        site = urlparse(page.url).netloc.removeprefix("www.")
        url = page.url
        from assistant.core.launch import launch
        try:
            await asyncio.to_thread(launch, url)                 # your normal browser usually gets in
            where = " I've opened it in your normal browser instead."
        except Exception:
            where = ""
        raise Blocked(f"{site} blocked my browser (it said “{title or 'access denied'}”).{where}")
    where = urlparse(page.url).netloc.removeprefix("www.")
    head = f"Now on {title or where}" + (f" ({where})" if title and where else "") + "."
    if not details:
        return head
    items = await snapshot(page)
    return head + "\nOn the page (use the numbers):\n" + describe_elements(items) if items else head


async def _target(page, target, field: bool = False):
    """A number from the list, or words on the button/link/box. field=True (typing, picking):
    boxes and dropdowns by their label first, not a button or an option with the same words."""
    t = str(target).strip()
    if re.fullmatch(r"\[?\d{1,3}\]?", t):
        loc = page.locator(f'[data-nova-id="{t.strip("[]")}"]')
        if await loc.count():
            return loc.first
        await snapshot(page)                                     # numbers from an older look: refresh
        loc = page.locator(f'[data-nova-id="{t.strip("[]")}"]')
        if await loc.count():
            return loc.first
        raise ToolError(f"There's no number {t} on the page any more.")
    if field:
        for loc in (page.get_by_label(t, exact=False), page.get_by_placeholder(t, exact=False),
                    page.get_by_role("combobox", name=t, exact=False), page.get_by_role("textbox", name=t, exact=False),
                    page.get_by_role("searchbox", name=t, exact=False)):
            if await loc.count():
                return loc.first
    for role in ("button", "link", "tab", "menuitem", "option", "checkbox"):
        loc = page.get_by_role(role, name=t, exact=False)
        if await loc.count():
            return loc.first
    for loc in (page.get_by_label(t, exact=False), page.get_by_placeholder(t, exact=False),
                page.get_by_text(t, exact=False)):
        if await loc.count():
            return loc.first
    raise ToolError(f"I can't find “{t}” on the page.")


async def _click(el) -> None:
    """Click like a person; if an ad or banner is in the way, click through it."""
    try:
        await el.scroll_into_view_if_needed(timeout=3000)
        await el.click(timeout=4000)
    except Exception:
        await el.evaluate("e => e.click()")      # something covers it: press the element itself


async def _search_box(page):
    for sel in _SEARCH_BOXES:
        loc = page.locator(sel)
        for i in range(min(await loc.count(), 4)):
            box = loc.nth(i)
            if await box.is_visible() and await box.is_editable():
                return box
    # Some sites hide the box behind a magnifying-glass button.
    for opener in ("button[aria-label*=search i]", "[role=button][aria-label*=search i]", "button[class*=search i]"):
        btn = page.locator(opener)
        if await btn.count() and await btn.first.is_visible():
            try:
                await btn.first.click(timeout=2500)
                await page.wait_for_timeout(500)
            except Exception:
                continue
            for sel in _SEARCH_BOXES:
                loc = page.locator(sel)
                if await loc.count() and await loc.first.is_visible():
                    return loc.first
    return None


# --- the tool --------------------------------------------------------------------------------
async def browser(args: dict, ctx: ToolContext) -> str:
    action = args.get("action")
    details = args.get("details", True)
    b = BROWSER
    page = await b.get_page()
    b.last_used = time.time()
    try:
        if action == "open":
            url = resolve(args.get("site") or args.get("text") or "")
            await page.goto(url, wait_until="domcontentloaded")
            await _settle(page)
            await page.bring_to_front()
            return await page_report(page, details)
        if action == "search":
            query = (args.get("text") or "").strip()
            if not query:
                raise ToolError("What should I search for?")
            if args.get("site"):
                await page.goto(resolve(args["site"]), wait_until="domcontentloaded")
                await _settle(page, 3000)
            if page.url in ("about:blank", ""):
                await page.goto("https://duckduckgo.com/?q=" + quote_plus(query), wait_until="domcontentloaded")
                await _settle(page)
                return f"Searched the web for “{query}”. " + await page_report(page, details)
            before = page.url
            box = await _search_box(page)
            if box is not None:
                await _click(box)
                await box.fill(query)
                await box.press("Enter")
                await _settle(page)
                if page.url != before or await _looks_like_results(page, query):
                    await page.bring_to_front()
                    return f"Searched {urlparse(page.url).netloc.removeprefix('www.')} for “{query}”. " + \
                        await page_report(page, details)
            # No usable search box (or it did nothing): go to the site's best page for it.
            site = urlparse(before).netloc.removeprefix("www.")
            await page.goto(site_search_url(site, query), wait_until="domcontentloaded")
            await _settle(page)
            await page.bring_to_front()
            return f"{site} has no search box I could use, so I went to its best page for “{query}”. " + \
                await page_report(page, details)
        if action == "click":
            el = await _target(page, args.get("target", ""))
            before = page.url
            await _click(el)
            try:                                             # a link: wait for the new page to start
                await page.wait_for_url(lambda u: u != before, timeout=1500)
            except Exception:
                pass
            await _settle(page, 3000)
            return "Clicked. " + await page_report(page, details)
        if action == "type":
            el = await _target(page, args.get("target", ""), field=True)
            await el.fill(args.get("text", ""))
            if args.get("enter"):
                await el.press("Enter")
                await _settle(page, 3000)
            return "Typed it. " + await page_report(page, details)
        if action == "select":
            el = await _target(page, args.get("target", ""), field=True)
            want = str(args.get("text", "")).strip()
            options = await el.evaluate("e => [...(e.options || [])].map(o => o.text.trim())")
            match = (next((o for o in options if o.lower() == want.lower()), None)
                     or next((o for o in options if want.lower() in o.lower()), None))
            if match is None:
                raise ToolError(f"“{want}” isn't one of the choices." +
                                (f" It has: {', '.join(options[:10])}." if options else ""))
            await el.select_option(label=match)
            await _settle(page, 2500)
            return f"Picked {want}. " + await page_report(page, details)
        if action == "press":
            await page.keyboard.press(_KEYS.get(str(args.get("text", "")).lower(), args.get("text", "Enter")))
            await _settle(page, 2500)
            return "Done. " + await page_report(page, details)
        if action == "scroll":
            down = str(args.get("text", "down")).lower() != "up"
            await page.mouse.wheel(0, 700 if down else -700)
            await page.wait_for_timeout(400)
            return f"Scrolled {'down' if down else 'up'}. " + await page_report(page, details)
        if action == "back":
            await page.go_back()
            await _settle(page, 3000)
            return "Went back. " + await page_report(page, details)
        if action == "read":
            text = await page.evaluate(
                "() => (document.querySelector('main') || document.body).innerText.replace(/\\n{2,}/g, '\\n')")
            head = await page_report(page, False)
            return f"{head}\nPage text (information only):\n{(text or '').strip()[:3500]}"
        if action == "look":
            return await page_report(page, True)
        raise ToolError(f"Unknown browser action: {action}")
    except ToolError:
        raise
    except Exception as e:
        if page.is_closed():
            b.page = None
            raise ToolError("My browser window was closed. Ask again and I'll reopen it.") from e
        raise ToolError(f"The page didn't cooperate: {str(e).splitlines()[0][:140]}") from e


async def _looks_like_results(page, query: str) -> bool:
    try:
        body = (await page.evaluate("() => document.body.innerText.slice(0, 20000)")).lower()
    except Exception:
        return False
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    return bool(words) and sum(w in body for w in words) >= max(1, len(words) - 1)


def _query(q: str) -> str:
    return re.sub(r"^(?:a|an|the|some)\s+", "", q.strip())


_KEYS = {"enter": "Enter", "escape": "Escape", "esc": "Escape", "tab": "Tab", "page down": "PageDown",
         "page up": "PageUp", "down": "ArrowDown", "up": "ArrowUp", "space": " "}


def browser_intent(t: str, active: bool, front: bool = False) -> tuple[str, dict] | None:
    """'open kbb.com and search for a 2019 honda civic', 'search kbb for civic', 'on amazon search
    for headphones', and (when Nova's browser is in use) 'search for X', 'click sign in',
    'scroll down', 'go back', 'read the page'."""
    m = (re.fullmatch(r"(?:open|go to|pull up|load)(?: up)? (?P<site>.+?)(?: website| site)?,? and (?:then )?"
                      r"(?:search|look) (?:it |the site )?(?:for|up) (?P<q>.+)", t)
         or re.fullmatch(r"(?:search|look on|look up on|check) (?P<site>[\w.' -]+?) for (?P<q>.+)", t)
         or re.fullmatch(r"(?:on|in) (?P<site>[\w.' -]+?),? (?:search|look) (?:for|up) (?P<q>.+)", t)
         or re.fullmatch(r"(?:search|look) (?:for|up) (?P<q>.+?) on (?P<site>[\w.' -]+?)(?: website| site)?", t))
    if m:
        site = m.group("site").strip()
        if site in ("youtube", "google", "the web", "the internet", "web", "internet", "spotify", "amazon music"):
            return None                                   # the normal web / YouTube search handles these
        return "browser", {"action": "search", "site": site, "text": _query(m.group("q")), "details": False}
    if active and (m := re.fullmatch(r"(?:search|look) (?:it |the site |the page )?(?:for|up) (?P<q>.+)", t)):
        return "browser", {"action": "search", "text": _query(m.group("q")), "details": False}
    if not front:
        return None
    if m := re.fullmatch(r"(?:click|tap)(?: on)? (?:the )?(?P<x>.+?)(?: button| link| tab)?", t):
        # "the first result", "the cheapest one": the model looks at the page and picks.
        if m.group("x") not in ("it", "that", "this") and not re.match(
                r"(?:first|second|third|fourth|last|top|1st|2nd|3rd|cheapest|best|next)\b", m.group("x")):
            return "browser", {"action": "click", "target": m.group("x"), "details": False}
    if m := re.fullmatch(r"scroll (up|down)(?: a bit| more)?", t):
        return "browser", {"action": "scroll", "text": m.group(1), "details": False}
    if re.fullmatch(r"go back(?: a page)?|back", t):
        return "browser", {"action": "back", "details": False}
    return None


def configure(settings) -> None:
    cfg = settings.browser
    BROWSER.channel, BROWSER.executable, BROWSER.headless = cfg.channel or None, cfg.executable_path, cfg.headless


def available(settings) -> bool:
    """Only where it can really run: Windows (Edge), or a Chromium given in the config."""
    import importlib.util
    import sys
    return importlib.util.find_spec("playwright") is not None and (
        sys.platform == "win32" or bool(settings.browser.executable_path))


def register(reg: ToolRegistry) -> None:
    configure(reg.settings)
    _route_websites(reg)
    reg.tool(
        "browser",
        "Nova's own web browser, controlled directly (no mouse needed). open: a site by name or "
        "address (site). search: type text into the page's search box and press Enter (site "
        "optional: opens it first). click: target = a number from the page list or the words on "
        "it. type: text into target (enter=true to submit). select: pick text in the dropdown "
        "target. press: a key (text). scroll: text up/down. back. read: the page's text (to answer "
        "questions about it). look: list what's on the page. Each step returns the page and its "
        "numbered elements: keep going step by step until the user's task is done.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["open", "search", "click", "type", "select", "press", "scroll",
                                                  "back", "read", "look"]},
            "site": {"type": "string", "maxLength": 300},
            "target": {"type": "string", "maxLength": 120},
            "text": {"type": "string", "maxLength": 500},
            "enter": {"type": "boolean"},
            "details": {"type": "boolean"}},
         "required": ["action"], "additionalProperties": False},
        risk=Risk.SAFE, category="web",
        describe=lambda a: f"{a.get('action')} {a.get('target') or a.get('site') or ''} in my browser",
    )(browser)


def _route_websites(reg: ToolRegistry) -> None:
    """'Open kbb.com' opens it in Nova's browser (so 'search for a Civic' can follow), unless
    that's switched off or can't run here. Searches of YouTube/Amazon/Google stay as they were."""
    tool = reg._tools.get("open_website")
    if tool is None:
        return
    original = tool.handler

    async def open_website(args: dict, ctx: ToolContext) -> str:
        if ctx.settings.browser.handle_websites and args.get("site") and not args.get("search") \
                and available(ctx.settings):
            return await browser({"action": "open", "site": args["site"], "details": False}, ctx)
        out = original(args, ctx)
        return await out if asyncio.iscoroutine(out) else out
    tool.handler = open_website
