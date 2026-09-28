"""Your own browser (Firefox, Chrome, Edge...), by voice: tabs, going places, back/forward,
finding and clicking things. Owner: "he still struggles doing simple tasks like open a new tab
on my browser, I want him to be able to control and navigate my browser effortlessly".

Why the old way failed: shortcuts went to whichever window was in front (Discord, the game,
Nova's own window), and "open a new tab on my browser" wasn't even understood. Now every action
  1. finds your browser (the one in front, else the one you used last),
  2. brings it to the front and checks it really is (Windows can refuse),
  3. does the thing with the browser's own shortcut, or clicks its tab by name,
  4. checks it happened (tab count and titles from UI Automation, the window title, the
     address bar) and only then says so.
Nova's own automated browser (tools/browser.py) is separate; when that one is in front, it
handles these itself.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry

BROWSER_PROCS = ("firefox", "chrome", "msedge", "brave", "opera", "vivaldi", "arc", "librewolf", "waterfox",
                 "zen", "floorp", "thorium")
STEP_S = 0.15              # between checks
WAIT_STEPS = 20            # ~3 s for a page to start loading / a tab to appear
_NEW_TAB_TITLES = ("new tab", "new private tab", "mozilla firefox", "google chrome", "start page", "speed dial")


# --- finding and reading the browser ----------------------------------------------------------
def is_browser(w) -> bool:
    return w is not None and w.process.lower().removesuffix(".exe") in BROWSER_PROCS


def _is_nova(w) -> bool:
    from assistant.tools import pc
    from assistant.tools.browser import BROWSER
    if pc.is_nova_window(w):
        return True
    page = BROWSER.page
    if page is None or page.is_closed() or "msedge" not in w.process.lower():
        return False
    title = (BROWSER.title or "").strip().lower()
    return bool(title) and w.title.lower().startswith(title[:40])


def label(w) -> str:
    from assistant.tools.keyboard import app_label
    return app_label(w)


def find_browser():
    """The user's browser window: the one in front if it's a browser, else the top-most one."""
    from assistant.tools import pc
    front = pc.WINDOWS.active()
    if is_browser(front) and not _is_nova(front):
        return front
    for w in pc.WINDOWS.list():                      # top of the pile first: the one used last
        if is_browser(w) and not _is_nova(w):
            return w
    raise ToolError("I can't see your browser. Say \"open Firefox\" first.")


def _title(hwnd: int) -> str | None:
    from assistant.tools import pc
    for w in pc.WINDOWS.list():
        if w.hwnd == hwnd:
            return w.title
    return None


def _page_title(title: str | None) -> str:
    """'YouTube — Mozilla Firefox' -> 'YouTube'."""
    t = title or ""
    return re.sub(r"\s+[-—–]\s+(?:Mozilla Firefox|Google Chrome|Microsoft​? Edge|Brave|Opera|Vivaldi)"
                  r"(?: Private Browsing)?$", "", t).strip()


def _tabs(hwnd: int) -> list[tuple[str, object, bool]] | None:
    """The tab strip (title, where, selected), or None when the browser doesn't say."""
    from assistant.tools import uia
    try:
        tabs = uia.UIA.tabs(hwnd)
    except Exception:
        return None
    tabs = [t for t in tabs if t[0]]
    if not tabs:
        return None
    # Only the browser's own tab strip: the top row (or, with vertical tabs, the left column).
    # Tabs inside a web page sit lower down and would throw the count off.
    top, left = min(t[1].y for t in tabs), min(t[1].x for t in tabs)
    return [t for t in tabs if t[1].y <= top + 12 or t[1].x <= left + 12]


def _address(hwnd: int) -> str | None:
    from assistant.tools import uia
    try:
        for v in uia.UIA.edit_values(hwnd):
            if uia._URLISH.match(v) and " " not in v:
                return v
    except Exception:
        return None
    return None


class _State:
    def __init__(self, hwnd: int):
        self.title = _title(hwnd)
        tabs = _tabs(hwnd)
        self.count = len(tabs) if tabs is not None else None
        self.address = _address(hwnd)

    def __eq__(self, other):
        return (self.title, self.count, self.address) == (other.title, other.count, other.address)


async def _read(hwnd: int) -> _State:
    return await asyncio.to_thread(_State, hwnd)


async def _until(hwnd: int, ok, steps: int = WAIT_STEPS) -> _State | None:
    """Re-read the browser until ok(state) (the page takes a moment to react)."""
    for _ in range(steps):
        await asyncio.sleep(STEP_S)
        st = await _read(hwnd)
        if ok(st):
            return st
    return None


# --- doing things ---------------------------------------------------------------------------
async def _bring(w) -> None:
    """Browser to the front, checked: keys must never land in another window."""
    from assistant.tools import pc
    fg = await asyncio.to_thread(lambda: pc.WINDOWS.foreground() if hasattr(pc.WINDOWS, "foreground") else w.hwnd)
    if fg == w.hwnd:
        return
    if not await asyncio.to_thread(pc.WINDOWS.focus, w.hwnd):
        raise ToolError(f"Windows wouldn't let me switch to {label(w)}. Click on it once, then ask again.")
    await asyncio.sleep(STEP_S)


async def _keys(spec: str) -> None:
    from assistant.tools import keyboard
    await asyncio.to_thread(keyboard.KEYBOARD.combo, keyboard.parse_keys(spec))
    await asyncio.sleep(0.05)


async def _type(text: str) -> None:
    from assistant.tools import keyboard
    await asyncio.to_thread(keyboard.KEYBOARD.type, text)


def destination(what: str) -> tuple[str, bool]:
    """What to put in the address bar: ('https://kbb.com', True) for a site, (words, False) for a
    search (the browser's own search engine)."""
    from assistant.tools import pc
    from assistant.tools.browser import NAMES
    s = what.strip().strip("\"'“”").rstrip(".")
    low = s.lower().removeprefix("the ").removesuffix(" website").removesuffix(" site").removesuffix(".com").strip()
    if low in NAMES:
        return "https://" + NAMES[low], True
    if low in pc.SITES:
        return pc.SITES[low], True
    if re.fullmatch(r"(?:https?://)?[\w-]+(?:\.[\w-]+)+(?:[/?#]\S*)?", s, re.I):
        return s if "://" in s else "https://" + s, True
    return s, False


def _nice(url: str) -> str:
    return urlparse(url if "://" in url else "https://" + url).netloc.removeprefix("www.") or url


def _match_tab(tabs, name: str):
    from assistant.tools import uia
    want = uia._norm(name.removesuffix(" tab"))
    scored = sorted(((uia.score(uia.Element(t[0], "tab", t[1]), want, None), i) for i, t in enumerate(tabs)),
                    key=lambda p: -p[0])
    return scored[0][1] if scored and scored[0][0] > 0 else None


async def _click(ctx: ToolContext, rect) -> None:
    from assistant.tools import grid
    g = grid.controller(ctx)
    await asyncio.to_thread(g.mouse.move, rect.x + rect.w // 2, rect.y + rect.h // 2)
    await asyncio.sleep(0.03)
    await asyncio.to_thread(g.mouse.click, "left", False)


async def _navigate(w, text: str, new_tab: bool) -> str:
    url, is_site = destination(text)
    before = await _read(w.hwnd)
    if new_tab:
        await _keys("ctrl+t")
        opened = await _until(w.hwnd, lambda st: st.title != before.title or st.count != before.count, steps=10)
        if opened is None:
            raise ToolError(f"I pressed Ctrl+T in {label(w)}, but no new tab appeared. Click on it once, then ask again.")
        before = opened
    else:
        await _keys("ctrl+l")                       # the address bar, text selected
        await asyncio.sleep(0.1)
    await _type(url)
    await _keys("enter")
    changed = await _until(w.hwnd, lambda st: st.title != before.title or st.address != before.address
                           or (before.count is not None and st.count != before.count))
    where = " in a new tab" if new_tab else ""
    if changed is None:
        raise ToolError(f"I typed it into {label(w)}'s address bar, but the page didn't change. "
                        "Click on the browser once, then ask again.")
    if is_site:
        return f"Opened {_nice(url)}{where} in {label(w)}."
    return f"Searched for {text}{where} in {label(w)}."


_COMBOS = {"new_tab": "ctrl+t", "close_tab": "ctrl+w", "next_tab": "ctrl+tab", "previous_tab": "ctrl+shift+tab",
           "back": "alt+left", "forward": "alt+right", "reload": "f5"}
ACTION_FOR = {**{v: k for k, v in _COMBOS.items()}, "ctrl+shift+t": "reopen_tab"}


async def my_browser(args: dict, ctx: ToolContext) -> str:
    action = args["action"]
    from assistant.tools import browser
    if action in _COMBOS and not args.get("go") and not args.get("tab") and not args.get("number") \
            and await asyncio.to_thread(browser.BROWSER.in_front):
        return await browser.shortcut(_COMBOS[action])       # Nova's own browser is the one in front
    w = await asyncio.to_thread(find_browser)
    name = label(w)
    await _bring(w)
    hwnd = w.hwnd

    if action == "new_tab":
        if args.get("go"):
            return await _navigate(w, args["go"], new_tab=True)
        before = await _read(hwnd)
        await _keys("ctrl+t")
        ok = await _until(hwnd, lambda st: (before.count is not None and st.count is not None and st.count > before.count)
                          or (st.title != before.title and _page_title(st.title).lower().startswith(_NEW_TAB_TITLES)))
        if ok is None:
            raise ToolError(f"I pressed Ctrl+T in {name}, but no new tab appeared. Click on {name} once, then ask again.")
        return f"Opened a new tab in {name}."

    if action == "go":
        if not args.get("go"):
            raise ToolError("Where to?")
        return await _navigate(w, args["go"], new_tab=False)

    if action in ("next_tab", "previous_tab"):
        before = await _read(hwnd)
        if before.count == 1:
            return f"There's only one tab open in {name}."
        await _keys("ctrl+tab" if action == "next_tab" else "ctrl+shift+tab")
        st = await _until(hwnd, lambda st: st.title != before.title, steps=8)
        if st is None:
            raise ToolError(f"I pressed the tab shortcut in {name}, but it stayed on the same tab.")
        return f"Now on {_page_title(st.title) or 'the next tab'}."

    if action == "switch_tab":
        return await _switch(w, args, ctx)

    if action == "close_tab":
        if args.get("tab") or args.get("number"):
            await _switch(w, args, ctx)
        before = await _read(hwnd)
        closing = _page_title(before.title)
        await _keys("ctrl+w")
        st = await _until(hwnd, lambda st: st.title != before.title or st.count != before.count or st.title is None)
        if st is None:
            raise ToolError(f"I pressed Ctrl+W in {name}, but the tab is still there.")
        return f"Closed {closing or 'the tab'}." if closing else "Closed the tab."

    if action == "reopen_tab":
        before = await _read(hwnd)
        await _keys("ctrl+shift+t")
        st = await _until(hwnd, lambda st: st.title != before.title or st.count != before.count)
        if st is None:
            raise ToolError(f"There was no closed tab for {name} to bring back.")
        return f"Brought back {_page_title(st.title) or 'the tab'}."

    if action in ("back", "forward", "reload"):
        before = await _read(hwnd)
        await _keys({"back": "alt+left", "forward": "alt+right", "reload": "f5"}[action])
        if action == "reload":
            return f"Reloaded {_page_title(before.title) or 'the page'}."
        st = await _until(hwnd, lambda st: st.title != before.title or st.address != before.address, steps=12)
        if st is None:
            raise ToolError(f"There's no page to go {action} to in this tab.")
        return f"{'Back' if action == 'back' else 'Forward'} to {_page_title(st.title) or 'the page'}."

    if action == "list_tabs":
        tabs = await asyncio.to_thread(_tabs, hwnd)
        if not tabs:
            return f"{name} didn't tell me its tabs. You're on {_page_title(_title(hwnd))}."
        names = [f"{i + 1}, {t[0][:50]}" + (" (this one)" if t[2] else "") for i, t in enumerate(tabs[:12])]
        more = f", and {len(tabs) - 12} more" if len(tabs) > 12 else ""
        return f"{len(tabs)} tab{'s' if len(tabs) != 1 else ''} open: " + "; ".join(names) + more + "."

    if action == "find":
        if not args.get("text"):
            raise ToolError("Find what?")
        await _keys("ctrl+f")
        await asyncio.sleep(0.15)
        await _keys("ctrl+a")
        await _type(args["text"])
        await _keys("enter")
        return f"Looking for \"{args['text']}\" on the page; it's highlighted if it's there."

    if action == "click":
        return await _click_on_page(w, args.get("text") or args.get("tab") or "", ctx)

    raise ToolError(f"I can't do '{action}' in the browser.")


async def _switch(w, args: dict, ctx: ToolContext) -> str:
    name, hwnd = label(w), w.hwnd
    number = args.get("number")
    before = await _read(hwnd)
    if number:
        n = int(number)
        tabs = await asyncio.to_thread(_tabs, hwnd)
        if tabs is not None and n != -1 and n > len(tabs):
            raise ToolError(f"There are only {len(tabs)} tabs in {name}.")
        if n == -1 or n >= 9:
            await _keys("ctrl+9")                           # the last tab
        else:
            await _keys(f"ctrl+{n}")
        st = await _until(hwnd, lambda st: st.title != before.title, steps=6)
        title = (st or before).title
        return f"Now on {_page_title(title) or f'tab {n}'}."
    want = (args.get("tab") or "").strip()
    if not want:
        raise ToolError("Which tab?")
    from assistant.tools import uia
    want_norm = uia._norm(want.removesuffix(" tab"))
    if want_norm and want_norm in uia._norm(_page_title(before.title)):
        return f"You're already on {_page_title(before.title)}."
    tabs = await asyncio.to_thread(_tabs, hwnd)
    if tabs:
        i = _match_tab(tabs, want)
        if i is None:
            raise ToolError(f"I can't see a {want} tab. Open tabs: " + ", ".join(t[0][:30] for t in tabs[:8]) + ".")
        await _click(ctx, tabs[i][1])
        st = await _until(hwnd, lambda st: st.title != before.title, steps=8)
        if st is None and not tabs[i][2]:
            raise ToolError(f"I clicked the {tabs[i][0][:40]} tab, but {name} stayed where it was.")
        return f"Now on {tabs[i][0][:60]}."
    # The browser doesn't list its tabs: go through them with Ctrl+Tab, reading each title.
    seen, last = {before.title}, before.title
    for _ in range(30):
        await _keys("ctrl+tab")
        st = await _until(hwnd, lambda st, last=last: st.title != last, steps=4)
        title = st.title if st else await asyncio.to_thread(_title, hwnd)
        if want_norm in uia._norm(_page_title(title)):
            return f"Now on {_page_title(title)}."
        if title in seen:
            break
        seen.add(title)
        last = title
    raise ToolError(f"I went through the tabs in {name} and didn't find {want}.")


async def _click_on_page(w, what: str, ctx: ToolContext) -> str:
    """Click a link or button on the page by its name (UI Automation), checked by what it did."""
    from assistant.tools import uia
    if not what:
        raise ToolError("Click what?")
    elements = await asyncio.to_thread(uia.UIA.elements, w.hwnd)
    hits = uia.best_matches(elements, what)
    if not hits:
        raise ToolError(f"I can't see '{what}' on the page in {label(w)}.")
    el = uia._reading_order(hits)[0]
    before = await _read(w.hwnd)
    await _click(ctx, el.rect)
    st = await _until(w.hwnd, lambda st: st.title != before.title or st.address != before.address, steps=8)
    if st is not None:
        return f"Clicked {el.name[:60] or what}. Now on {_page_title(st.title)}."
    return f"Clicked {el.name[:60] or what}."


def register(reg: ToolRegistry) -> None:
    reg.tool(
        "my_browser",
        "The user's own web browser (Firefox etc.), even when it isn't in front: new_tab (go = where, "
        "optional), go (go = site or words to search, in this tab), switch_tab (tab = its name, or number), "
        "close_tab (optionally tab/number), next_tab, previous_tab, reopen_tab, back, forward, reload, "
        "list_tabs, find (text on the page), click (text = link or button name). Use this for 'new tab', "
        "'go to the YouTube tab', 'go back'. Every action is checked.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["new_tab", "go", "switch_tab", "close_tab", "next_tab",
                                                  "previous_tab", "reopen_tab", "back", "forward", "reload",
                                                  "list_tabs", "find", "click"]},
            "go": {"type": "string", "maxLength": 300},
            "tab": {"type": "string", "maxLength": 100},
            "number": {"type": "integer", "minimum": -1, "maximum": 99},
            "text": {"type": "string", "maxLength": 200}},
         "required": ["action"], "additionalProperties": False},
        risk=Risk.SAFE, category="web",
    )(my_browser)


# --- what people say --------------------------------------------------------------------------
_BR = r"(?: (?:on|in) (?:my |the |your )?(?:browser|web browser|firefox|chrome|edge|brave|opera))?"
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
             "last": -1, "final": -1}
_NOT_A_TAB = ("this", "that", "the", "current", "my", "new", "a", "")


def _num(word: str) -> int | None:
    return int(word) if word.isdigit() else _WORD_NUM.get(word)


def tab_intent(t: str) -> tuple[str, dict] | None:
    """'open a new tab on my browser', 'open youtube in a new tab', 'go to the spotify tab',
    'close the youtube tab', 'go to tab 3', 'what tabs do I have open', 'find pricing on this page'."""
    if re.fullmatch(r"(?:(?:open|make|create|start|give me|pull up|bring up|get me|add|put up|can i get|i need|i want)"
                    r"(?: up)?(?: me)? )?(?:a |another |one more )?(?:new|fresh|blank|empty|another) tab(?: please)?" + _BR
                    + r"|(?:open|make|create|add) (?:up )?(?:a|another) tab" + _BR, t):
        return "my_browser", {"action": "new_tab"}
    m = (re.fullmatch(r"(?:open|pull up|bring up|load|go to|put) (.+?) (?:in|on) (?:a )?(?:new|another|separate) tab" + _BR, t)
         or re.fullmatch(r"(?:(?:open|make) )?(?:a )?new tab(?: and| then|,)? (?:go to|open|with|for|to|search(?: for)?|"
                         r"and search(?: for)?|look up|google) (.+?)" + _BR, t))
    if m:
        return "my_browser", {"action": "new_tab", "go": m.group(1)}
    if m := re.fullmatch(r"(?:go to|open|load|search(?: for)?) (.+?) in (?:this|the same|the current) tab", t):
        return "my_browser", {"action": "go", "go": m.group(1)}
    if re.fullmatch(r"close (?:this |the |that |my |current |the current )?tab" + _BR, t):
        return "my_browser", {"action": "close_tab"}
    if m := re.fullmatch(r"close (?:the )?tab (?:number )?(\w+)", t):
        if (n := _num(m.group(1))) is not None:
            return "my_browser", {"action": "close_tab", "number": n}
    if m := re.fullmatch(r"close (?:the |my )?(.+?) tab" + _BR, t):
        if m.group(1) not in _NOT_A_TAB:
            return "my_browser", {"action": "close_tab", "tab": m.group(1)}
    if m := re.fullmatch(r"(?:go|switch|change|take me|jump|flip|move)(?: back)? to (?:the )?(?:tab (?:number )?(\w+)|"
                         r"(\w+) tab)" + _BR, t):
        n = _num(m.group(1) or m.group(2) or "")
        if n is not None and (m.group(1) or m.group(2) in _WORD_NUM):
            return "my_browser", {"action": "switch_tab", "number": n}
    if m := re.fullmatch(r"(?:go|switch|change|take me|jump|flip|move|bring me)(?: back)? to (?:the |my )?(.+?) tab" + _BR
                         + r"|(?:open|show me|pull up) (?:the |my )?(.+?) tab", t):
        name = m.group(1) or m.group(2)
        if name not in _NOT_A_TAB and name not in ("next", "previous", "last", "other"):
            return "my_browser", {"action": "switch_tab", "tab": name}
    if re.fullmatch(r"(?:go to |switch to )?(?:the )?next tab|tab right", t):
        return "my_browser", {"action": "next_tab"}
    if re.fullmatch(r"(?:go to |switch to |go back to )?(?:the )?(?:previous|last|other) tab|tab left", t):
        return "my_browser", {"action": "previous_tab"}
    if re.fullmatch(r"(?:reopen|bring back|restore|undo close|open back up)(?: the| that| my)?(?: last| closed| last closed)* tab"
                    r"|(?:reopen|bring back) (?:the tab|what i closed)|undo (?:closing|close) (?:the |that )?tab", t):
        return "my_browser", {"action": "reopen_tab"}
    if re.fullmatch(r"what tabs (?:do i have|have i got|are)(?: open)?|(?:list|read|show|tell)(?: me)? (?:all )?(?:my |the )?(?:open )?tabs"
                    r"|how many tabs (?:do i have|have i got|are)(?: open)?|which tabs are open", t):
        return "my_browser", {"action": "list_tabs"}
    if m := re.fullmatch(r"(?:find|search for|look for|search) (.+?) on (?:this|the) (?:page|site|website)", t):
        return "my_browser", {"action": "find", "text": m.group(1)}
    m = re.fullmatch(r"(go back|go forward|refresh|reload)(?: the page)?(?: (?:on|in) (?:my |the )?(?:browser|firefox|chrome|edge))", t)
    if m:
        return "my_browser", {"action": {"go back": "back", "go forward": "forward"}.get(m.group(1), "reload")}
    return None
