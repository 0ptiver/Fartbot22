"""Any app, by voice: see what's in it, click things, type into boxes, press keys. Owner: "I want
him to be able to control and navigate my browser and any app for that matter effortlessly".

Works through UI Automation (what screen readers use): every normal app says what buttons,
boxes, tabs and menu items it has and where. `look` lists them numbered (like Nova's own
browser does for web pages), so the model can go step by step: look -> click 4 -> type.
The app is brought to the front first and checked, so keys never land in another window.
Games don't expose anything: there the grid works ("show the grid").
"""

from __future__ import annotations

import asyncio
import re

from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry

MAX_LISTED = 60
_LAST: dict = {"hwnd": None, "elements": []}          # the last `look`, for "click 4"


def _window(app: str | None):
    from assistant.tools import pc
    return pc._find_window(app) if app else pc.active_window()


def _label(w) -> str:
    from assistant.tools.keyboard import app_label
    return app_label(w)


async def _front(w) -> None:
    """The app to the front, checked (it may be minimised or behind Nova's window)."""
    from assistant.tools import pc
    fg = await asyncio.to_thread(lambda: pc.WINDOWS.foreground() if hasattr(pc.WINDOWS, "foreground") else w.hwnd)
    if fg == w.hwnd:
        return
    if not await asyncio.to_thread(pc.WINDOWS.focus, w.hwnd):
        raise ToolError(f"Windows wouldn't let me switch to {_label(w)}. Click on it once, then ask again.")
    await asyncio.sleep(0.2)


async def _elements(w) -> list:
    from assistant.tools import uia
    els = await asyncio.to_thread(uia.UIA.elements, w.hwnd)
    return [e for e in uia._reading_order(els) if e.name.strip()]


async def _find(w, target: str):
    """An element by number (from the last look) or by name."""
    from assistant.tools import uia
    t = str(target).strip()
    if t.isdigit():
        n = int(t)
        if _LAST["hwnd"] != w.hwnd or not 1 <= n <= len(_LAST["elements"]):
            raise ToolError(f"I don't have a number {n} for {_label(w)}. Look at it first.")
        return _LAST["elements"][n - 1]
    elements = await _elements(w)
    hits = uia.best_matches(elements, t)
    if not hits:
        names = ", ".join(dict.fromkeys(e.name[:25] for e in elements[:12]))
        raise ToolError(f"I can't see '{t}' in {_label(w)}." + (f" I can see: {names}." if names else
                                                               " It doesn't tell Windows what's in it."))
    return uia._reading_order(hits)[0]


async def _click(ctx: ToolContext, el, button: str = "left", double: bool = False) -> None:
    from assistant.tools import grid
    g = grid.controller(ctx)
    await asyncio.to_thread(g.mouse.move, el.rect.x + el.rect.w // 2, el.rect.y + el.rect.h // 2)
    await asyncio.sleep(0.03)
    await asyncio.to_thread(g.mouse.click, button, double)


async def app_control(args: dict, ctx: ToolContext) -> str:
    from assistant.tools import keyboard
    action = args["action"]
    w = await asyncio.to_thread(_window, args.get("app"))
    name = _label(w)
    await _front(w)

    if action == "look":
        elements = (await _elements(w))[:MAX_LISTED]
        if not elements:
            raise ToolError(f"{name} doesn't tell Windows what's in it (games usually don't). "
                            "Say \"show the grid\" to click anywhere.")
        _LAST.update(hwnd=w.hwnd, elements=elements)
        listed = "; ".join(f'[{i}] {e.kind} "{e.name[:50]}"' for i, e in enumerate(elements, 1))
        return f"{name} ({w.title[:60]}): {listed}. Use click with a number or name."

    if action in ("click", "double_click", "right_click"):
        if not args.get("target"):
            raise ToolError("Click what?")
        el = await _find(w, args["target"])
        await _click(ctx, el, "right" if action == "right_click" else "left", action == "double_click")
        return f"Clicked {el.name[:60]} in {name}."

    if action == "type":
        text = args.get("text") or ""
        if not text:
            raise ToolError("Type what?")
        field = ""
        if args.get("target"):
            el = await _find(w, args["target"])
            await _click(ctx, el)
            await asyncio.sleep(0.15)
            field = el.name[:40]
        await keyboard._guard(ctx, "type_text", {"text": text}, risky=True)
        await asyncio.to_thread(keyboard.KEYBOARD.type, text)
        if args.get("enter"):
            await asyncio.to_thread(keyboard.KEYBOARD.combo, [keyboard.VK["enter"]])
        return f"Typed \"{text[:60]}\"" + (f" into {field}" if field else f" in {name}") + \
            (" and pressed Enter." if args.get("enter") else ".")

    if action == "press":
        keys = args.get("keys") or ""
        vks = keyboard.parse_keys(keys)
        await keyboard._guard(ctx, "press_keys", {"keys": keys}, risky=vks[-1] == keyboard.VK["enter"])
        times = max(1, min(int(args.get("times") or 1), 20))
        for _ in range(times):
            await asyncio.to_thread(keyboard.KEYBOARD.combo, vks)
            await asyncio.sleep(0.03)
        return f"Pressed {keys}" + (f" {times} times" if times > 1 else "") + f" in {name}."

    raise ToolError(f"I can't '{action}' in an app.")


def register(reg: ToolRegistry) -> None:
    reg.tool(
        "app",
        "Use any app on the PC (app = its name, e.g. 'discord', 'spotify', 'settings'; empty = the one "
        "in front). look = list its buttons, boxes, tabs and menu items, numbered; click / double_click / "
        "right_click target (a name or a number from look); type text (into target, a box's name, "
        "optional; enter=true to press Enter after); press keys ('ctrl+k', 'enter', 'escape'). "
        "For a task in an app: look first, then act step by step.",
        {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["look", "click", "double_click", "right_click", "type", "press"]},
            "app": {"type": "string", "maxLength": 80},
            "target": {"type": "string", "maxLength": 120},
            "text": {"type": "string", "maxLength": 2000},
            "enter": {"type": "boolean"},
            "keys": {"type": "string", "maxLength": 40},
            "times": {"type": "integer", "minimum": 1, "maximum": 20}},
         "required": ["action"], "additionalProperties": False},
        risk=Risk.SAFE, category="apps",
    )(app_control)


_FIELD = r"(?:box|field|bar|search|search bar|search box|chat|message box|text box)"


def app_intent(raw: str, t: str) -> tuple[str, dict] | None:
    """'click send in discord', 'in spotify click shuffle', 'type hello into the search box',
    'type gg in discord'."""
    m = (re.fullmatch(r"(click|double click|right click)(?: on)? (?:the )?(.+?) (?:in|on) (?:the |my )?([a-z0-9 ]{2,30}?)(?: app| window)?", t)
         or re.fullmatch(r"(?:in|on) (?:the |my )?([a-z0-9 ]{2,30}?)(?: app| window)?,? (click|double click|right click)(?: on)? (?:the )?(.+)", t))
    if m:
        if m.group(1) in ("click", "double click", "right click"):
            verb, target, app = m.group(1), m.group(2), m.group(3)
        else:
            app, verb, target = m.group(1), m.group(2), m.group(3)
        if app not in ("this", "the page", "this page", "the screen", "screen", "the grid") and \
                not re.search(r"\b(?:page|screen|video|grid)$", app):
            return "app", {"action": verb.replace(" ", "_"), "target": target, "app": app}
    m = re.match(r"^\s*(?:please\s+|can you\s+|could you\s+)?(?:type|write|enter|put)\s+(.+?)\s+(?:in|into|in to)\s+"
                 r"(?:the |my )?(.+?)[.!?]?\s*$", raw, re.I | re.S)
    if m:
        text, where = m.group(1).strip().strip("\"'“”"), m.group(2).strip().lower()
        if re.search(r"\b" + _FIELD + r"$", where):
            return "app", {"action": "type", "text": text, "target": where}
        if len(where.split()) <= 2 and where not in ("it", "this", "that", "there", "here"):
            return "app", {"action": "type", "text": text, "app": where}
    return None
