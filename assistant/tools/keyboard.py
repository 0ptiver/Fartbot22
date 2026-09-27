"""Keyboard by voice: type text, press keys and shortcuts ("press control c", "new tab").

Safety: typing or pressing Enter into a terminal (PowerShell, cmd, Windows Terminal) or the
Run box runs commands, so that asks first (and never happens remotely). Everything is audited.
"""

from __future__ import annotations

import re
import sys
import time

from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry

MAX_TYPE_CHARS = 2000

# Spoken or written key names -> virtual-key codes.
VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B,
    "start": 0x5B, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
    "space": 0x20, "spacebar": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "capslock": 0x14,
    "printscreen": 0x2C, "menu": 0x5D, "plus": 0xBB, "equals": 0xBB, "minus": 0xBD,
    "comma": 0xBC, "period": 0xBE, "dot": 0xBE, "slash": 0xBF,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
}
MODIFIERS = {0x11, 0x10, 0x12, 0x5B}
_EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B, 0x5D}
_ALIASES = {"page up": "pageup", "page down": "pagedown", "caps lock": "capslock",
            "print screen": "printscreen", "up arrow": "up", "down arrow": "down",
            "left arrow": "left", "right arrow": "right", "arrow up": "up", "arrow down": "down",
            "arrow left": "left", "arrow right": "right", "windows key": "win", "start key": "win",
            "the windows key": "win", "space bar": "space", "back space": "backspace",
            "escape key": "escape", "enter key": "enter", "full stop": "period"}
TERMINALS = ("windowsterminal", "powershell", "pwsh", "cmd", "conhost", "openconsole", "wt",
             "mintty", "wsl", "bash")


def parse_keys(spec: str) -> list[int]:
    """'ctrl+shift+t', 'control c', 'alt f4', 'page down' -> [vk, ...] (modifiers first)."""
    s = spec.lower().strip()
    for k, v in _ALIASES.items():
        s = re.sub(rf"\b{k}\b", v, s)
    s = re.sub(r"\bf (\d{1,2})\b", r"f\1", s)                 # "f 4" -> f4
    parts = [p for p in re.split(r"[\s+]+|\band\b", s) if p and p not in ("key", "the", "button")]
    if not parts:
        raise ToolError("Which key?")
    vks = []
    for p in parts:
        if p not in VK:
            raise ToolError(f"I don't know the key '{p}'.")
        vks.append(VK[p])
    mods = [v for v in vks if v in MODIFIERS]
    keys = [v for v in vks if v not in MODIFIERS]
    if len(keys) > 1:
        raise ToolError("One key at a time (plus control/shift/alt/windows).")
    return list(dict.fromkeys(mods)) + keys


class KeyboardBackend:
    """SendInput via ctypes. Replaced by a fake in tests."""

    def _send(self, events: list[tuple[int, int, int]]) -> None:
        """events: (vk, scan, flags)."""
        if sys.platform != "win32":
            raise ToolError("Keyboard control only works on Windows.")
        import ctypes
        from ctypes import wintypes

        ULONG_PTR = ctypes.c_size_t

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        arr = (INPUT * len(events))()
        for i, (vk, scan, flags) in enumerate(events):
            arr[i].type = 1                                        # INPUT_KEYBOARD
            arr[i].u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))  # type: ignore[attr-defined]

    def combo(self, vks: list[int]) -> None:
        down = [(v, 0, 1 if v in _EXTENDED else 0) for v in vks]
        up = [(v, 0, (1 if v in _EXTENDED else 0) | 2) for v in reversed(vks)]
        self._send(down + up)

    def type(self, text: str) -> None:
        events = []
        for ch in text:
            if ch == "\n":
                events += [(0x0D, 0, 0), (0x0D, 0, 2)]
                continue
            data = ch.encode("utf-16-le")
            for i in range(0, len(data), 2):                       # emoji etc. are two units
                unit = int.from_bytes(data[i:i + 2], "little")
                events += [(0, unit, 4), (0, unit, 4 | 2)]         # KEYEVENTF_UNICODE (+ KEYUP)
        for i in range(0, len(events), 200):                       # modest batches
            self._send(events[i:i + 200])
            time.sleep(0.01)


KEYBOARD = KeyboardBackend()


def _into_terminal(ctx: ToolContext) -> str | None:
    """The window that would receive the keys, if it can run commands."""
    from assistant.tools import pc
    try:
        w = pc.WINDOWS.active()
    except ToolError:
        return None
    if w is None:
        return None
    proc = w.process.lower().removesuffix(".exe")
    if proc in TERMINALS or w.title.strip().lower() == "run":
        return w.title or proc
    return None


async def _guard(ctx: ToolContext, tool: str, args: dict, risky: bool) -> None:
    """Keys into a terminal/Run box can run programs: ask first, never from a phone."""
    import asyncio
    if not risky:
        return
    target = await asyncio.to_thread(_into_terminal, ctx)
    if target is None:
        return
    if ctx.remote:
        raise ToolError("I don't type into a command window from a remote device.")
    if not (ctx.confirm and await ctx.confirm(tool, {**args, "window": target})):
        raise ToolError("The user declined typing into the command window.")


async def type_text(args: dict, ctx: ToolContext) -> str:
    import asyncio
    text = args["text"]
    if len(text) > MAX_TYPE_CHARS:
        raise ToolError(f"That's too long to type ({len(text)} characters).")
    await _guard(ctx, "type_text", args, risky=True)
    await asyncio.to_thread(KEYBOARD.type, text)
    return "Typed." if len(text) > 40 else f"Typed: {text}"


async def press_keys(args: dict, ctx: ToolContext) -> str:
    import asyncio
    vks = parse_keys(args["keys"])
    times = max(1, min(int(args.get("times") or 1), 20))
    # Enter (or a shortcut with no modifiers other than shift) into a terminal runs what's typed.
    risky = vks[-1] == 0x0D or VK["win"] in vks and vks[-1] == VK["r"]
    await _guard(ctx, "press_keys", args, risky=risky)
    for _ in range(times):
        await asyncio.to_thread(KEYBOARD.combo, vks)
        if times > 1:
            await asyncio.sleep(0.03)
    return f"Pressed {args['keys']}" + (f" {times} times." if times > 1 else ".")


def register(reg: ToolRegistry) -> None:
    reg.tool("type_text", "Type text into the app in front, as if typed on the keyboard. Asks first "
             "when that app is a terminal.",
             {"type": "object", "properties": {"text": {"type": "string", "minLength": 1,
                                                        "maxLength": MAX_TYPE_CHARS}},
              "required": ["text"], "additionalProperties": False}, risk=Risk.SAFE, category="keyboard",
             describe=lambda a: f"type \"{a.get('text', '')[:60]}\" into {a.get('window', 'the command window')}",
             )(type_text)
    reg.tool("press_keys", "Press a key or shortcut in the app in front: 'enter', 'ctrl+c', 'alt+tab', "
             "'ctrl+shift+t', 'win+left', 'page down', 'f5'. times = repeat (max 20).",
             {"type": "object", "properties": {"keys": {"type": "string", "minLength": 1, "maxLength": 40},
                                               "times": {"type": "integer", "minimum": 1, "maximum": 20}},
              "required": ["keys"], "additionalProperties": False}, risk=Risk.SAFE, category="keyboard",
             describe=lambda a: f"press {a.get('keys', '')} in {a.get('window', 'the command window')}",
             )(press_keys)
