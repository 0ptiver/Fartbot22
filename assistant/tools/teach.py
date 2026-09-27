"""Teach by showing: "Nova, watch what I do" ... "Nova, done" ... "call it morning setup".

Recording: while watching, Nova checks the mouse buttons and keys ~100 times a second (no
keyboard hook, so nothing can lag or glitch). For every click it notes *what* was clicked
(the button's name, via UI Automation) and in which app, not just where; so replaying still
works after windows move. Things Nova itself did on request during the lesson ("Nova, open
Steam") are recorded as those actions.

Privacy: typing into a password box is never recorded. Clicks on Nova's own window are
ignored. Lessons are saved on this PC only (data/learned.json) and can be deleted.

Replay: the lesson becomes a routine ("Nova, morning setup"), run through the normal tool
registry, so the usual safety rules apply (typing into a terminal asks first, etc.).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from assistant.core.config import ROOT
from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry

log = logging.getLogger(__name__)

LEARNED_FILE = ROOT / "data" / "learned.json"
MAX_EVENTS = 400
MAX_SECONDS = 600
MOD_VKS = {0x10: "shift", 0x11: "ctrl", 0x12: "alt", 0x5B: "win", 0x5C: "win"}
_SKIP_VKS = set(range(0x01, 0x07)) | {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,
                                      0xE7, 0xFF}          # mouse buttons, modifiers, VK_PACKET
# Things Nova did on request during a lesson that are worth repeating as-is.
REPEATABLE_TOOLS = {"open_app", "open_website", "window", "volume", "video", "music_control", "play_music",
                    "media_key", "type_text", "set_voice", "subtitles", "press_key"}


# --- recording ------------------------------------------------------------------------------------
@dataclass
class Event:
    kind: str                    # down | up | key | tool
    t: float
    button: str = ""
    x: int = 0
    y: int = 0
    vk: int = 0
    mods: tuple[str, ...] = ()
    target: dict | None = None   # what's under the mouse at 'down': window + element
    password: bool = False
    tool: str = ""
    args: dict = field(default_factory=dict)


class InputPoller:
    """Polls GetAsyncKeyState (no hooks). Windows only; a fake in tests."""

    def __init__(self, describe_point: Callable[[int, int], dict | None] | None = None,
                 focused_is_password: Callable[[], bool] | None = None, interval: float = 0.01):
        self.describe_point = describe_point or describe_point_uia
        self.focused_is_password = focused_is_password or focused_is_password_uia
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, emit: Callable[[Event], None]) -> None:
        if sys.platform != "win32":
            raise ToolError("Learning by watching only works on Windows.")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(emit,), name="teach-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)

    def _run(self, emit: Callable[[Event], None]) -> None:
        import ctypes
        from ctypes import wintypes

        from assistant.tools.grid import _dpi_aware
        try:
            import comtypes
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:
            pass
        _dpi_aware()
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.GetAsyncKeyState.restype = ctypes.c_short         # bit 15 = held down right now
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        state = user32.GetAsyncKeyState
        down: set[int] = {vk for vk in range(1, 255) if state(vk) & 0x8000}   # already held: ignore
        pt = wintypes.POINT()
        pw_until = 0.0
        pw = False
        while not self._stop.is_set():
            now = time.monotonic()
            user32.GetCursorPos(ctypes.byref(pt))
            for vk, name in ((0x01, "left"), (0x02, "right"), (0x04, "middle")):
                pressed = bool(state(vk) & 0x8000)
                if pressed and vk not in down:
                    down.add(vk)
                    emit(Event("down", now, button=name, x=pt.x, y=pt.y, target=self.describe_point(pt.x, pt.y)))
                    pw_until = 0.0                     # focus may have changed: re-check password
                elif not pressed and vk in down:
                    down.discard(vk)
                    emit(Event("up", now, button=name, x=pt.x, y=pt.y))
            mods = tuple(sorted({MOD_VKS[m] for m in MOD_VKS if state(m) & 0x8000}))
            for vk in range(0x08, 0xFF):
                if vk in _SKIP_VKS:
                    continue
                pressed = bool(state(vk) & 0x8000)
                if pressed and vk not in down:
                    down.add(vk)
                    if now > pw_until:
                        pw, pw_until = self.focused_is_password(), now + 1.0
                    emit(Event("key", now, vk=vk, mods=mods, password=pw))
                elif not pressed and vk in down:
                    down.discard(vk)
            time.sleep(self.interval)


def _uia():
    import comtypes.client
    mod = comtypes.client.GetModule("UIAutomationCore.dll")
    return mod, comtypes.client.CreateObject(mod.CUIAutomation, interface=mod.IUIAutomation)


def describe_point_uia(x: int, y: int) -> dict | None:
    """Which app, and which named button/link/box, is at this point."""
    from assistant.tools import pc
    from assistant.tools.uia import CONTROL_TYPES
    try:
        w = pc.WINDOWS.at(x, y)
    except Exception:
        w = None
    if w is None:
        return None
    info: dict[str, Any] = {"window": {"process": w.process, "title": w.title}}
    try:
        left, top, width, height = pc.WINDOWS.rect(w.hwnd)
        info["rel"] = [round((x - left) / max(width, 1), 4), round((y - top) / max(height, 1), 4)]
    except Exception:
        pass
    try:
        mod, uia = _uia()
        e = uia.ElementFromPoint(mod.tagPOINT(x, y))
        walker = uia.ControlViewWalker
        for _ in range(4):                          # an icon inside a button: use the button
            if e is None:
                break
            name, ctype = (e.CurrentName or "").strip(), e.CurrentControlType
            if name and ctype in CONTROL_TYPES:
                info["element"] = {"name": name[:120], "kind": CONTROL_TYPES[ctype]}
                break
            e = walker.GetParentElement(e)
    except Exception as ex:
        log.debug("describe_point: %s", ex)
    return info


def focused_is_password_uia() -> bool:
    try:
        _, uia = _uia()
        return bool(uia.GetFocusedElement().CurrentIsPassword)
    except Exception:
        return False


# --- events -> steps --------------------------------------------------------------------------------
def build_steps(events: list[Event]) -> tuple[list[dict], bool]:
    """(routine steps, whether some password typing was left out)."""
    steps: list[dict] = []
    skipped_pw = False
    last_t: float | None = None
    pending_down: dict[str, Event] = {}
    typing: list[list] = []

    def gap(t: float) -> None:
        nonlocal last_t
        if last_t is not None and t - last_t > 0.4:
            steps.append({"wait": round(min(t - last_t, 3.0), 1)})
        last_t = t

    def flush_typing() -> None:
        nonlocal typing
        if typing:
            steps.append({"tool": "replay_keys", "args": {"keys": typing, "shows": keys_text(typing)}})
            typing = []

    for ev in events:
        if ev.kind == "down":
            pending_down[ev.button] = ev
            continue
        if ev.kind == "up":
            d = pending_down.pop(ev.button, None)
            if d is None or not d.target:
                continue
            if is_nova_target(d.target):
                continue
            flush_typing()
            prev = steps[-1] if steps else None
            moved = abs(ev.x - d.x) + abs(ev.y - d.y) > 12
            if (not moved and prev and prev.get("tool") == "replay_click" and not prev["args"].get("double")
                    and prev["args"]["button"] == d.button and ev.t - prev["args"].get("_t", 0) < 0.45
                    and abs(prev["args"]["abs"][0] - d.x) + abs(prev["args"]["abs"][1] - d.y) < 8):
                prev["args"]["double"] = True
                last_t = ev.t
                continue
            gap(d.t)
            args = {"button": d.button, "double": False, "window": d.target.get("window"),
                    "element": d.target.get("element"), "rel": d.target.get("rel"), "abs": [d.x, d.y], "_t": ev.t}
            if moved:
                args["drag_to"] = [ev.x, ev.y]
            steps.append({"tool": "replay_click", "args": args})
            last_t = ev.t
            continue
        if ev.kind == "key":
            if ev.password:
                skipped_pw = True
                continue
            combo = [m for m in ev.mods if m != "shift"]
            if combo:                                  # a shortcut: its own step
                flush_typing()
                gap(ev.t)
                steps.append({"tool": "replay_keys", "args": {"keys": [[ev.vk, list(ev.mods)]],
                                                              "shows": keys_text([[ev.vk, list(ev.mods)]])}})
                last_t = ev.t
            else:
                if not typing:
                    gap(ev.t)
                typing.append([ev.vk, list(ev.mods)])
                last_t = ev.t
            continue
        if ev.kind == "tool":
            flush_typing()
            gap(ev.t)
            steps.append({"tool": ev.tool, "args": ev.args})
            last_t = ev.t
    flush_typing()
    for s in steps:
        if s.get("tool") == "replay_click":
            s["args"].pop("_t", None)
    while steps and "wait" in steps[-1]:
        steps.pop()
    return steps, skipped_pw


def is_nova_target(target: dict) -> bool:
    w = (target or {}).get("window") or {}
    return (w.get("title") or "").strip().lower() == "nova"


_NAMES = {0x0D: "Enter", 0x09: "Tab", 0x1B: "Esc", 0x08: "Backspace", 0x2E: "Delete", 0x20: " ",
          0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓", 0x21: "PageUp", 0x22: "PageDown", 0x24: "Home", 0x23: "End"}
_SHIFTED = dict(zip("1234567890", "!@#$%^&*()"))
_OEM = {0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`", 0xDB: "[", 0xDC: "\\",
        0xDD: "]", 0xDE: "'"}


def keys_text(keys: list[list]) -> str:
    """What a key sequence looks like, for descriptions: 'gta[Enter]', 'Ctrl+C'."""
    out = []
    for vk, mods in keys:
        mods = list(mods)
        shift = "shift" in mods
        combo = [m.capitalize() if m != "win" else "Win" for m in mods if m != "shift"]
        if 0x41 <= vk <= 0x5A:
            ch = chr(vk) if shift else chr(vk).lower()
        elif 0x30 <= vk <= 0x39:
            ch = _SHIFTED[chr(vk)] if shift else chr(vk)
        elif vk in _OEM:
            ch = _OEM[vk]
        elif 0x70 <= vk <= 0x7B:
            ch = f"F{vk - 0x6F}"
        else:
            ch = _NAMES.get(vk, "?")
        if combo:
            out.append("[" + "+".join(combo + [ch.upper() if len(ch) == 1 else ch]) + "]")
        elif len(ch) > 1 and ch not in ("←", "↑", "→", "↓"):
            out.append(f"[{ch}]")
        else:
            out.append(ch)
    return "".join(out)


def describe_step(step: dict) -> str:
    if "wait" in step:
        return f"wait {step['wait']:g} s"
    tool, a = step.get("tool"), step.get("args", {})
    if tool == "replay_click":
        app = ((a.get("window") or {}).get("process") or "the screen").removesuffix(".exe")
        what = f"\"{a['element']['name']}\"" if a.get("element") else "a spot"
        verb = "drag" if a.get("drag_to") else "double-click" if a.get("double") else \
            "right-click" if a.get("button") == "right" else "click"
        return f"{verb} {what} in {app}"
    if tool == "replay_keys":
        return f"type {a.get('shows', '')}"
    detail = ", ".join(f"{v}" for v in a.values() if isinstance(v, (str, int, float)))
    return tool.replace("_", " ") + (f" ({detail})" if detail else "")


# --- saved lessons -----------------------------------------------------------------------------------
def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40]


def load_learned(path: Path | None = None) -> dict[str, dict]:
    try:
        data = json.loads((path or LEARNED_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_learned(data: dict[str, dict], path: Path | None = None) -> None:
    path = path or LEARNED_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


class Teacher:
    """The lesson in progress: recording -> waiting for a name -> saved."""

    def __init__(self, reg: ToolRegistry, poller: Any = None, clock: Callable[[], float] = time.monotonic):
        self.reg, self.poller, self.clock = reg, poller or InputPoller(), clock
        self.recording = False
        self.awaiting_name = False
        self.events: list[Event] = []
        self.pending: list[dict] = []
        self.started = 0.0
        self._lock = threading.Lock()

    def _emit(self, ev: Event) -> None:
        with self._lock:
            if self.recording and len(self.events) < MAX_EVENTS and ev.t - self.started < MAX_SECONDS:
                self.events.append(ev)

    def _on_tool(self, name: str, args: dict, ok: bool) -> None:
        if ok and name in REPEATABLE_TOOLS:
            self._emit(Event("tool", self.clock(), tool=name, args=dict(args)))

    def start(self) -> str:
        if self.recording:
            return "I'm already watching. Say 'Nova, done' when you've finished."
        self.events, self.pending, self.awaiting_name = [], [], False
        self.started = self.clock()
        self.recording = True
        try:
            self.poller.start(self._emit)
        except Exception:
            self.recording = False
            raise
        self.reg.observers.append(self._on_tool)
        return "Watching. Do it now, and say 'Nova, done' when you've finished."

    def stop(self) -> str:
        if not self.recording:
            return "I wasn't watching anything."
        self.recording = False
        self.poller.stop()
        if self._on_tool in self.reg.observers:
            self.reg.observers.remove(self._on_tool)
        steps, skipped_pw = build_steps(self.events)
        self.events = []
        real = [s for s in steps if "tool" in s]
        if not real:
            return "I didn't see you do anything, so there's nothing to save."
        self.pending, self.awaiting_name = steps, True
        pw = " I left out what you typed in the password box." if skipped_pw else ""
        first = "; ".join(describe_step(s) for s in real[:3]) + ("; and so on" if len(real) > 3 else "")
        return f"Got it: {len(real)} step{'s' if len(real) != 1 else ''}: {first}.{pw} What should I call it?"

    def save(self, name: str) -> str:
        name = " ".join(re.sub(r"^(?:call it|name it|save it as|it's called|its called)\s+", "", name.strip(),
                               flags=re.I).split()).strip(" .!?\"'")
        if not self.pending:
            return "There's nothing waiting to be saved. Say 'Nova, watch what I do' to teach me something."
        if not 2 <= len(name) <= 40 or not slug(name):
            return "Give it a short name, like 'morning setup'."
        data = load_learned()
        data[slug(name)] = {"phrases": [name.lower()], "reply": "Done.", "steps": self.pending,
                            "learned": True, "created": time.time()}
        save_learned(data)
        self.pending, self.awaiting_name = [], False
        return f"Saved. Say 'Nova, {name}' and I'll do it."

    def cancel(self) -> str:
        was = self.recording or self.awaiting_name
        if self.recording:
            self.recording = False
            self.poller.stop()
            if self._on_tool in self.reg.observers:
                self.reg.observers.remove(self._on_tool)
        self.events, self.pending, self.awaiting_name = [], [], False
        return "Forgotten; nothing saved." if was else "There was nothing to cancel."


def delete_learned(name: str) -> str:
    data = load_learned()
    key = slug(re.sub(r"\b(?:the|routine|lesson)\b", "", name))
    hit = key if key in data else next((k for k in data if key and key in k), None)
    if hit is None:
        return f"I haven't learned anything called {name}."
    del data[hit]
    save_learned(data)
    return f"Forgotten: {hit.replace('_', ' ')}."


# --- replay tools -----------------------------------------------------------------------------------
WINDOW_WAIT_S = 8.0


async def _find_window(spec: dict | None, timeout: float | None = None):
    """The recorded app's window, waiting for it to open (it may be starting)."""
    from assistant.tools import pc
    if not spec or not spec.get("process"):
        return None
    want_p, want_t = spec["process"].lower(), (spec.get("title") or "").lower()
    deadline = time.monotonic() + (WINDOW_WAIT_S if timeout is None else timeout)
    while True:
        wins = [w for w in await asyncio.to_thread(pc.WINDOWS.list) if w.process.lower() == want_p]
        if wins:
            return next((w for w in wins if w.title.lower() == want_t), wins[0])
        if time.monotonic() > deadline:
            raise ToolError(f"{spec['process'].removesuffix('.exe')} isn't open, so I couldn't do that step.")
        await asyncio.sleep(0.5)


async def replay_click(args: dict, ctx: ToolContext) -> str:
    from assistant.tools import grid, pc, uia
    g = grid.controller(ctx)
    win = await _find_window(args.get("window"))
    target = None
    if win is not None:
        await asyncio.to_thread(pc.WINDOWS.show, win.hwnd, "focus")
        await asyncio.sleep(0.3)
        left, top, width, height = await asyncio.to_thread(pc.WINDOWS.rect, win.hwnd)
        el = args.get("element")
        if el and el.get("name"):
            deadline = time.monotonic() + 4.0
            while target is None:                      # the button may take a moment to appear
                elements = await asyncio.to_thread(uia.UIA.elements, win.hwnd)
                hits = [e for e in uia.best_matches(elements, el["name"])
                        if uia.score(e, uia._norm(el["name"]), None) >= 80]
                if hits:
                    rel = args.get("rel") or [0.5, 0.5]
                    want = (left + rel[0] * width, top + rel[1] * height)
                    best = min(hits, key=lambda e: (e.rect.x + e.rect.w / 2 - want[0]) ** 2
                               + (e.rect.y + e.rect.h / 2 - want[1]) ** 2)
                    target = (best.rect.x + best.rect.w // 2, best.rect.y + best.rect.h // 2)
                elif time.monotonic() > deadline:
                    break
                else:
                    await asyncio.sleep(0.4)
        if target is None and args.get("rel"):
            rel = args["rel"]
            target = (int(left + rel[0] * width), int(top + rel[1] * height))
    if target is None:
        target = tuple(args["abs"])
    await asyncio.to_thread(g.mouse.move, *target)
    await asyncio.sleep(0.03)
    if args.get("drag_to"):
        dx, dy = args["drag_to"][0] - args["abs"][0], args["drag_to"][1] - args["abs"][1]
        await asyncio.to_thread(g.mouse.drag, target[0], target[1], target[0] + dx, target[1] + dy)
        return "Dragged."
    await asyncio.to_thread(g.mouse.click, args.get("button", "left"), bool(args.get("double")))
    return "Clicked."


async def replay_keys(args: dict, ctx: ToolContext) -> str:
    from assistant.tools import keyboard
    await keyboard._guard(ctx, "replay_keys", {"keys": args.get("shows", "")}, risky=True)
    for vk, mods in args["keys"][:500]:
        combo = [keyboard.VK[m] for m in mods if m in keyboard.VK] + [int(vk)]
        await asyncio.to_thread(keyboard.KEYBOARD.combo, combo)
        await asyncio.sleep(0.012)
    return "Typed."


# --- the teach tool + phrases -------------------------------------------------------------------------
def register(reg: ToolRegistry) -> None:
    def teacher(ctx: ToolContext) -> Teacher:
        t = ctx.services.get("teacher")
        if t is None:
            t = ctx.services["teacher"] = Teacher(reg)
        return t

    def teach(args: dict, ctx: ToolContext) -> str:
        a = args["action"]
        t = teacher(ctx)
        if a == "start":
            return t.start()
        if a == "stop":
            return t.stop()
        if a == "save":
            return t.save(args.get("name", ""))
        if a == "cancel":
            return t.cancel()
        if a == "delete":
            return delete_learned(args.get("name", ""))
        learned = load_learned()
        if not learned:
            return "You haven't taught me anything yet. Say 'Nova, watch what I do'."
        return "I've learned: " + ", ".join(v["phrases"][0] for v in learned.values()) + "."

    reg.tool("teach", "Learn a task by watching the user: start (watch what I do), stop (done), save "
             "(name it), cancel, delete (a learned task), list.",
             {"type": "object", "properties": {
                 "action": {"type": "string", "enum": ["start", "stop", "save", "cancel", "delete", "list"]},
                 "name": {"type": "string", "maxLength": 60}},
              "required": ["action"], "additionalProperties": False}, risk=Risk.SAFE, category="teach")(teach)
    reg.tool("replay_click", "(Used by learned tasks.) Click a recorded button in a recorded app.",
             {"type": "object"}, risk=Risk.SAFE, category="teach")(replay_click)
    reg.tool("replay_keys", "(Used by learned tasks.) Type recorded keys.",
             {"type": "object", "properties": {"keys": {"type": "array"}, "shows": {"type": "string"}},
              "required": ["keys"]}, risk=Risk.SAFE, category="teach")(replay_keys)


def teach_intent(t: str, teacher: Teacher | None = None) -> tuple[str, dict] | None:
    """Phrases for teaching. While recording, 'done'; while waiting for a name, any short answer."""
    if teacher is not None and teacher.recording:
        if re.fullmatch(r"(?:ok |okay |right |alright )?(?:i'?m )?(?:done|finished|that'?s it|that'?s all|"
                        r"stop (?:watching|recording)(?: me)?|all done|end (?:of )?(?:lesson|recording))", t):
            return "teach", {"action": "stop"}
        if re.fullmatch(r"(?:cancel|never mind|forget it|scrap that|stop,? forget it)", t):
            return "teach", {"action": "cancel"}
    if teacher is not None and teacher.awaiting_name:
        if re.fullmatch(r"(?:cancel|never mind|forget it|don'?t save it|no|nothing)", t):
            return "teach", {"action": "cancel"}
        if len(t.split()) <= 6:
            return "teach", {"action": "save", "name": t}
    if re.fullmatch(r"(?:watch|look at) (?:what i do|me|this|how i do (?:this|it))|learn (?:this|something new|how to do this)"
                    r"|(?:let me|i'?ll|i want to) (?:show|teach) you (?:something|how.*|this)|teach you something"
                    r"|(?:start )?record(?:ing)? (?:this|what i do|me)|learn by watching", t):
        return "teach", {"action": "start"}
    m = re.fullmatch(r"(?:call it|name it|save it as) (.{2,40})", t)
    if m:
        return "teach", {"action": "save", "name": m.group(1)}
    m = re.fullmatch(r"(?:forget|delete|remove) (?:the |my )?(.+?) (?:routine|lesson|task)", t)
    if m:
        return "teach", {"action": "delete", "name": m.group(1)}
    if re.fullmatch(r"what (?:have|did) you learn(?:ed|t)?|what have i taught you|(?:list|show) (?:what you'?ve learned|my lessons)", t):
        return "teach", {"action": "list"}
    return None
