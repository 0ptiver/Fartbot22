"""Click things by name ("click Subscribe", "click the search box") and "show numbers".

Uses Windows UI Automation, the accessibility interface screen readers use: every app says
what buttons, links, boxes and tabs it has, where they are, and what they're called. That's
instant and exact, with no screenshot or AI. (Games usually don't expose anything; use the
grid there.)
"""

from __future__ import annotations

import difflib
import re
import sys
from dataclasses import dataclass
from functools import reduce

from assistant.tools.grid import Region, _dpi_aware, controller
from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry

# UI Automation ids (UIAutomationClient.h)
_NAME, _CTYPE, _RECT, _OFFSCREEN, _ENABLED = 30005, 30003, 30001, 30022, 30010
_DESCENDANTS = 4
CONTROL_TYPES = {50000: "button", 50002: "checkbox", 50003: "combobox", 50004: "box", 50005: "link",
                 50007: "item", 50011: "menu item", 50013: "radio button", 50019: "tab",
                 50024: "item", 50029: "item", 50031: "button", 50018: "button"}
_KIND_WORDS = {"button": "button", "link": "link", "tab": "tab", "box": "box", "field": "box",
               "bar": "box", "checkbox": "checkbox", "check box": "checkbox", "menu": "menu item",
               "option": "item", "item": "item", "icon": "button"}
MAX_NUMBERS = 150


@dataclass
class Element:
    name: str
    kind: str
    rect: Region


class UIABackend:
    """UI Automation via comtypes (already installed for the volume control). A fake in tests."""

    def elements(self, hwnd: int) -> list[Element]:
        if sys.platform != "win32":
            raise ToolError("Clicking by name only works on Windows.")
        _dpi_aware()
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)   # this worker thread
        except OSError:
            pass
        mod = comtypes.client.GetModule("UIAutomationCore.dll")
        uia = comtypes.client.CreateObject(mod.CUIAutomation, interface=mod.IUIAutomation)
        root = uia.ElementFromHandle(hwnd)
        cache = uia.CreateCacheRequest()
        for pid in (_NAME, _CTYPE, _RECT):
            cache.AddProperty(pid)
        kinds = reduce(uia.CreateOrCondition, [uia.CreatePropertyCondition(_CTYPE, t) for t in CONTROL_TYPES])
        cond = uia.CreateAndCondition(kinds, uia.CreateAndCondition(
            uia.CreatePropertyCondition(_OFFSCREEN, False), uia.CreatePropertyCondition(_ENABLED, True)))
        found = root.FindAllBuildCache(_DESCENDANTS, cond, cache)   # one cross-process call
        out: list[Element] = []
        for i in range(min(found.Length, 3000)):
            e = found.GetElement(i)
            r = e.CachedBoundingRectangle
            left, top, right, bottom = ((r.left, r.top, r.right, r.bottom) if hasattr(r, "left") else r)
            if right - left < 4 or bottom - top < 4:
                continue
            out.append(Element((e.CachedName or "").strip(), CONTROL_TYPES.get(e.CachedControlType, "item"),
                               Region(left, top, right - left, bottom - top)))
        return out


    def edit_values(self, hwnd: int) -> list[str]:
        """The text in each box of a window (a browser's address bar among them)."""
        if sys.platform != "win32":
            return []
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass
        mod = comtypes.client.GetModule("UIAutomationCore.dll")
        uia = comtypes.client.CreateObject(mod.CUIAutomation, interface=mod.IUIAutomation)
        cache = uia.CreateCacheRequest()
        cache.AddProperty(30045)                                   # Value.Value
        found = uia.ElementFromHandle(hwnd).FindAllBuildCache(
            _DESCENDANTS, uia.CreatePropertyCondition(_CTYPE, 50004), cache)
        out = []
        for i in range(min(found.Length, 50)):
            try:
                v = found.GetElement(i).GetCachedPropertyValue(30045)
            except Exception:
                continue
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        return out


UIA = UIABackend()
_URLISH = re.compile(r"^(?:https?://)?(?:[\w-]+\.)+[a-z]{2,}(?:[/?#:]\S*)?$", re.I)


def current_url(w=None) -> str:
    """The address of the page in the browser you're using (or in window `w`)."""
    from assistant.tools import pc
    w = w or pc.active_window()
    if w.process.lower().removesuffix(".exe") not in ("firefox", "chrome", "msedge", "brave", "opera", "vivaldi"):
        raise ToolError("Open the page in your browser first, then ask again.")
    for v in UIA.edit_values(w.hwnd):
        if _URLISH.match(v) and " " not in v:
            return v if v.startswith("http") else "https://" + v
    raise ToolError("I couldn't read the address of that page. Tell me the website instead.")


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", s.lower()).split())


def split_kind(query: str) -> tuple[str, str | None]:
    """'the search box' -> ('search', 'box'); 'subscribe button' -> ('subscribe', 'button')."""
    q = _norm(re.sub(r"^(?:on )?(?:the |a |an |that |this )", "", query.strip().lower()))
    for word in sorted(_KIND_WORDS, key=len, reverse=True):
        if q.endswith(" " + word) or q == word:
            return q[: -len(word)].strip(), _KIND_WORDS[word]
    return q, None


def score(el: Element, want: str, kind: str | None) -> float:
    name = _norm(el.name)
    if not name or not want:
        return 0.0
    if name == want:
        s = 100.0
    elif name.startswith(want + " ") or want.startswith(name + " "):
        s = 80.0
    elif all(w in name.split() for w in want.split()):
        s = 70.0 - min(len(name.split()) - len(want.split()), 10)
    elif want in name:
        s = 55.0
    else:
        ratio = difflib.SequenceMatcher(None, want, name[: len(want) + 6]).ratio()
        s = 50.0 * ratio if ratio >= 0.8 else 0.0
    if s and kind and el.kind == kind:
        s += 10
    return s


def best_matches(elements: list[Element], query: str) -> list[Element]:
    """The top-scoring elements (several if they tie)."""
    want, kind = split_kind(query)
    if not want and kind:                       # just "the search box": any box, if only one
        pool = [e for e in elements if e.kind == kind]
        return pool
    scored = sorted(((score(e, want, kind), e) for e in elements), key=lambda p: -p[0])
    scored = [(s, e) for s, e in scored if s > 0]
    if not scored:
        return []
    top = scored[0][0]
    return [e for s, e in scored if s >= top - 0.5]


def _reading_order(elements: list[Element]) -> list[Element]:
    return sorted(elements, key=lambda e: (e.rect.y // 12, e.rect.x))


def _window_elements() -> tuple[str, list[Element]]:
    from assistant.tools import pc
    w = pc.active_window()
    label = w.process.removesuffix(".exe") or w.title
    return label, UIA.elements(w.hwnd)


def click_element(args: dict, ctx: ToolContext) -> str:
    g = controller(ctx)
    label, elements = _window_elements()
    hits = best_matches(elements, args["name"])
    if not hits:
        raise ToolError(f"I can't see '{args['name']}' in {label}. Say 'show numbers' to see what I "
                        "can click, or 'show the grid'.")
    if len(hits) > 1:
        hits = _reading_order(hits)[:20]
        g.show_labels([e.rect for e in hits])
        return f"I see {len(hits)} of those. Say 'click' and a number."
    el = hits[0]
    g.labels = {1: el.rect}
    g.act(args.get("action", "click"), 1)
    return f"Clicked {el.name or 'it'}."


def show_numbers(args: dict, ctx: ToolContext) -> str:
    g = controller(ctx)
    label, elements = _window_elements()
    elements = _reading_order(elements)[:MAX_NUMBERS]
    if not elements:
        raise ToolError(f"{label} doesn't tell Windows what's clickable (games usually don't). "
                        "Say 'show the grid' instead.")
    g.show_labels([e.rect for e in elements])
    if "numbers" in g.explained:
        return "Numbers on."
    g.explained.add("numbers")
    return f"Numbers on: {len(elements)} things. Say 'click' and a number."


def register(reg: ToolRegistry) -> None:
    reg.tool("click_element", "Click a button, link, tab, box or menu item by its name in the app "
             "in front, e.g. 'Subscribe', 'search box', 'Sign in'. If several match, numbers appear.",
             {"type": "object", "properties": {
                 "name": {"type": "string", "minLength": 1, "maxLength": 100},
                 "action": {"type": "string", "enum": ["click", "double_click", "right_click"]}},
              "required": ["name"], "additionalProperties": False}, risk=Risk.SAFE, category="mouse")(click_element)
    reg.tool("show_numbers", "Put a number on everything clickable in the app in front; then the user "
             "says 'click 7'.", risk=Risk.SAFE, category="mouse")(show_numbers)
