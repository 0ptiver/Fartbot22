"""Voice mouse: a numbered grid over the screen, then "click 14", "zoom 14", "scroll down".

Like Windows Voice Access's mouse grid. The first grid covers the screen the mouse is on
(or "screen 2", "the other screen"); "zoom N" splits cell N into a 3x3 grid for precision.
Clicking hides the grid again.

The overlay is click-through and never takes focus, so clicks and key presses still go to
the app underneath. All Win32 calls live in small backends so the logic is tested with fakes.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field

from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry

log = logging.getLogger(__name__)
IS_WINDOWS = sys.platform == "win32"


@dataclass
class Region:
    x: int
    y: int
    w: int
    h: int


@dataclass
class Grid:
    region: Region
    cols: int
    rows: int

    @property
    def count(self) -> int:
        return self.cols * self.rows

    def cell(self, n: int) -> Region:
        """Cells are numbered 1.. left to right, top to bottom."""
        if not 1 <= n <= self.count:
            raise ToolError(f"Pick a number from 1 to {self.count}.")
        r, c = divmod(n - 1, self.cols)
        x0 = self.region.x + round(c * self.region.w / self.cols)
        x1 = self.region.x + round((c + 1) * self.region.w / self.cols)
        y0 = self.region.y + round(r * self.region.h / self.rows)
        y1 = self.region.y + round((r + 1) * self.region.h / self.rows)
        return Region(x0, y0, x1 - x0, y1 - y0)

    def center(self, n: int) -> tuple[int, int]:
        c = self.cell(n)
        return c.x + c.w // 2, c.y + c.h // 2


# --- backends ------------------------------------------------------------------------------------
class MouseBackend:
    """Win32 mouse via ctypes. Replaced by a fake in tests."""

    def _user32(self):
        if not IS_WINDOWS:
            raise ToolError("The mouse grid only works on Windows.")
        import ctypes
        return ctypes.windll.user32  # type: ignore[attr-defined]

    def monitors(self) -> list[Region]:
        """Every screen in real pixels: the main one first, then the others left to right."""
        import ctypes
        from ctypes import wintypes

        u = self._user32()
        _dpi_aware()

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        found: list[tuple[bool, Region]] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                            ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        def cb(hmon, _hdc, _rect, _data):
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if u.GetMonitorInfoW(hmon, ctypes.byref(info)):
                r = info.rcMonitor
                found.append((bool(info.dwFlags & 1), Region(r.left, r.top, r.right - r.left, r.bottom - r.top)))
            return True

        u.EnumDisplayMonitors(None, None, cb, 0)
        if not found:
            return [Region(0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1))]
        return order_screens(found)

    def virtual(self) -> Region:
        """All screens together (for numbers that can be on any screen)."""
        u = self._user32()
        _dpi_aware()
        return Region(u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79))

    def cursor(self) -> tuple[int, int]:
        import ctypes
        from ctypes import wintypes
        pt = wintypes.POINT()
        self._user32().GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def move(self, x: int, y: int) -> None:
        _dpi_aware()
        self._user32().SetCursorPos(int(x), int(y))

    def click(self, button: str = "left", double: bool = False) -> None:
        u = self._user32()
        down, up = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}[button]
        for _ in range(2 if double else 1):
            u.mouse_event(down, 0, 0, 0, 0)
            u.mouse_event(up, 0, 0, 0, 0)
            if double:
                time.sleep(0.05)

    def scroll(self, notches: int) -> None:
        self._user32().mouse_event(0x0800, 0, 0, int(notches) * 120, 0)   # + up, - down

    def drag(self, x0: int, y0: int, x1: int, y1: int) -> None:
        u = self._user32()
        self.move(x0, y0)
        u.mouse_event(0x2, 0, 0, 0, 0)
        steps = 12
        for i in range(1, steps + 1):          # move in steps so apps see a real drag
            time.sleep(0.02)
            self.move(x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps)
        u.mouse_event(0x4, 0, 0, 0, 0)


def order_screens(found: list[tuple[bool, Region]]) -> list[Region]:
    """Screen 1 = the main screen, then the rest from left to right."""
    main = [r for primary, r in found if primary]
    rest = sorted((r for primary, r in found if not primary), key=lambda r: (r.x, r.y))
    return main + rest


def pick_screen(screens: list[Region], which: str | int | None, cursor: tuple[int, int] | None,
                current: Region | None = None) -> Region:
    """which: None (the screen the mouse is on), 1.., 'next', 'left', 'right', 'main'."""
    if which in (None, ""):
        if cursor:
            for r in screens:
                if r.x <= cursor[0] < r.x + r.w and r.y <= cursor[1] < r.y + r.h:
                    return r
        return screens[0]
    w = str(which).lower()
    if w in ("main", "primary", "first"):
        return screens[0]
    if w in ("next", "other"):
        if current in screens:
            return screens[(screens.index(current) + 1) % len(screens)]
        return screens[1 % len(screens)]
    if w == "left":
        return min(screens, key=lambda r: r.x)
    if w == "right":
        return max(screens, key=lambda r: r.x)
    if w.isdigit() and 1 <= int(w) <= len(screens):
        return screens[int(w) - 1]
    n = len(screens)
    raise ToolError("You only have one screen." if n == 1 else f"Pick a screen from 1 to {n}.")


_dpi_done = False


def _dpi_aware() -> None:
    """Use real pixels (not Windows' scaled ones) so the grid and the clicks line up."""
    global _dpi_done
    if _dpi_done or not IS_WINDOWS:
        return
    _dpi_done = True
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # type: ignore[attr-defined]
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()    # type: ignore[attr-defined]
        except Exception:
            pass


class TkOverlay:
    """A see-through, click-through, always-on-top window that draws the grid.
    Tk runs on its own thread; other threads send it commands through a queue."""

    KEY = "#ff00fe"                    # this colour is made transparent

    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._error: str | None = None
        self._ready = threading.Event()

    def _start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="grid-overlay", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        if self._error:
            raise ToolError(self._error)

    def show(self, grid: Grid, screen: Region | None = None) -> None:
        self._start()
        self._q.put(("show", (grid, screen or grid.region)))

    def show_labels(self, labels: dict[int, Region], screen: Region) -> None:
        self._start()
        self._q.put(("labels", (labels, screen)))

    def hide(self) -> None:
        if self._thread and self._thread.is_alive():
            self._q.put(("hide", None))

    def _run(self) -> None:
        try:
            _dpi_aware()
            import tkinter as tk
            root = tk.Tk()
        except Exception as e:                   # no Tk in this Python
            self._error = f"I can't draw the grid on this PC ({e})."
            self._ready.set()
            return
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=self.KEY)
        root.attributes("-transparentcolor", self.KEY)
        canvas = tk.Canvas(root, bg=self.KEY, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        root.withdraw()
        root.update_idletasks()
        self._click_through(root)
        self._ready.set()

        def draw(grid: Grid, screen: Region) -> None:
            canvas.delete("all")
            # The window covers one screen; drawing is relative to that screen's corner.
            root.geometry(f"{screen.w}x{screen.h}+{screen.x}+{screen.y}")
            ox, oy = screen.x, screen.y
            g0 = grid
            grid = Grid(Region(g0.region.x - ox, g0.region.y - oy, g0.region.w, g0.region.h), g0.cols, g0.rows)
            r = grid.region
            canvas.create_rectangle(r.x, r.y, r.x + r.w, r.y + r.h, outline="#000000", width=4)
            canvas.create_rectangle(r.x, r.y, r.x + r.w, r.y + r.h, outline="#5ad8ff", width=2)
            for c in range(1, grid.cols):
                x = r.x + round(c * r.w / grid.cols)
                canvas.create_line(x, r.y, x, r.y + r.h, fill="#000000", width=3)
                canvas.create_line(x, r.y, x, r.y + r.h, fill="#5ad8ff", width=1)
            for rr in range(1, grid.rows):
                y = r.y + round(rr * r.h / grid.rows)
                canvas.create_line(r.x, y, r.x + r.w, y, fill="#000000", width=3)
                canvas.create_line(r.x, y, r.x + r.w, y, fill="#5ad8ff", width=1)
            size = 16 if min(r.w // grid.cols, r.h // grid.rows) > 60 else 11
            for n in range(1, grid.count + 1):
                cx, cy = grid.center(n)
                t = canvas.create_text(cx, cy, text=str(n), fill="#ffffff",
                                       font=("Segoe UI", size, "bold"))
                x0, y0, x1, y1 = canvas.bbox(t)
                bg = canvas.create_rectangle(x0 - 6, y0 - 2, x1 + 6, y1 + 2, fill="#0b1020",
                                             outline="#5ad8ff")
                canvas.tag_raise(t, bg)
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
            self._click_through(root)

        def draw_labels(labels: dict[int, Region], screen: Region) -> None:
            canvas.delete("all")
            root.geometry(f"{screen.w}x{screen.h}+{screen.x}+{screen.y}")
            for n, r in labels.items():
                x, y = r.x - screen.x, r.y - screen.y
                canvas.create_rectangle(x, y, x + r.w, y + r.h, outline="#5ad8ff", width=1)
                t = canvas.create_text(x + 3, y + 2, text=str(n), anchor="nw", fill="#ffffff",
                                       font=("Segoe UI", 10, "bold"))
                x0, y0, x1, y1 = canvas.bbox(t)
                bg = canvas.create_rectangle(x0 - 3, y0 - 1, x1 + 3, y1 + 1, fill="#0b1020", outline="#5ad8ff")
                canvas.tag_raise(t, bg)
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
            self._click_through(root)

        def poll() -> None:
            try:
                while True:
                    cmd, arg = self._q.get_nowait()
                    if cmd == "labels":
                        draw_labels(*arg)
                    elif cmd == "show":
                        draw(*arg)
                    elif cmd == "hide":
                        root.withdraw()
            except queue.Empty:
                pass
            root.after(40, poll)

        root.after(40, poll)
        root.mainloop()

    @staticmethod
    def _click_through(root) -> None:
        """Mouse clicks go straight through to the app below; the overlay never takes focus."""
        if not IS_WINDOWS:
            return
        import ctypes
        u = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = u.GetParent(root.winfo_id()) or root.winfo_id()
        GWL_EXSTYLE = -20
        style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        # LAYERED | TRANSPARENT (click-through) | TOOLWINDOW (no taskbar) | NOACTIVATE | TOPMOST
        u.SetWindowLongW(hwnd, GWL_EXSTYLE, style | 0x80000 | 0x20 | 0x80 | 0x08000000 | 0x8)


# --- controller ----------------------------------------------------------------------------------
@dataclass
class GridController:
    mouse: object = field(default_factory=MouseBackend)
    overlay: object = field(default_factory=TkOverlay)
    cols: int = 10
    rows: int = 6
    grid: Grid | None = None
    history: list[Grid] = field(default_factory=list)
    screen: Region | None = None
    labels: dict[int, Region] | None = None      # "show numbers": clickable things, numbered

    @property
    def visible(self) -> bool:
        return self.grid is not None or self.labels is not None

    def show_labels(self, regions: list[Region]) -> None:
        self.grid, self.history = None, []
        self.labels = {i + 1: r for i, r in enumerate(regions)}
        self.overlay.show_labels(self.labels, self.mouse.virtual())

    def show(self, which: str | int | None = None) -> str:
        screens = self.mouse.monitors()
        cursor = self.mouse.cursor() if which in (None, "") else None
        self.screen = pick_screen(screens, which, cursor, self.screen if self.grid else None)
        self.grid = Grid(self.screen, self.cols, self.rows)
        self.history, self.labels = [], None
        self.overlay.show(self.grid, self.screen)
        where = f" on screen {screens.index(self.screen) + 1}" if len(screens) > 1 else " on"
        return (f"Grid{where}. Say 'click' and a number from 1 to {self.grid.count}, "
                "or 'zoom' and a number to get closer.")

    def hide(self) -> str:
        was_labels = self.labels is not None
        self.grid, self.history, self.labels = None, [], None
        self.overlay.hide()
        return "Numbers off." if was_labels else "Grid off."

    def _need(self) -> Grid:
        if self.labels is not None:
            raise ToolError("Say 'click' and the number.")
        if self.grid is None:
            raise ToolError("The grid isn't showing. Say 'show the grid' first.")
        return self.grid

    def zoom(self, n: int) -> str:
        g = self._need()
        cell = g.cell(n)
        if cell.w < 18 or cell.h < 18:
            raise ToolError("That's as close as it goes. Say 'click' and a number.")
        self.history.append(g)
        self.grid = Grid(cell, 3, 3)
        self.overlay.show(self.grid, self.screen)
        return f"Zoomed in on {n}."

    def back(self) -> str:
        if not self.history:
            return self.hide()
        self.grid = self.history.pop()
        self.overlay.show(self.grid, self.screen)
        return "Zoomed out."

    def point(self, n: int | None) -> tuple[int, int] | None:
        if n is None:
            return None
        if self.labels is not None:
            r = self.labels.get(n)
            if r is None:
                raise ToolError(f"Pick a number from 1 to {len(self.labels)}.")
            return r.x + r.w // 2, r.y + r.h // 2
        return self._need().center(n)

    def act(self, action: str, n: int | None = None, amount: int = 3, to: int | None = None) -> str:
        where = self.point(n) if n is not None else None
        if action in ("click", "double_click", "right_click", "middle_click"):
            self.overlay.hide()                    # never click (or screenshot) the grid itself
            time.sleep(0.05)
            if where:
                self.mouse.move(*where)
                time.sleep(0.03)
            button = {"right_click": "right", "middle_click": "middle"}.get(action, "left")
            self.mouse.click(button, double=action == "double_click")
            self.grid, self.history, self.labels = None, [], None
            return {"click": "Clicked.", "double_click": "Double-clicked.", "right_click": "Right-clicked.",
                    "middle_click": "Middle-clicked."}[action]
        if action == "move":
            if where is None:
                raise ToolError("Move to which number?")
            self.mouse.move(*where)
            return f"Mouse on {n}."
        if action in ("scroll_up", "scroll_down"):
            if where:
                self.mouse.move(*where)
            amount = max(1, min(int(amount or 3), 30))
            self.mouse.scroll(amount if action == "scroll_up" else -amount)
            return "Scrolled."
        if action == "drag":
            if where is None or to is None:
                raise ToolError("Drag from which number to which?")
            dest = self.point(to)
            self.overlay.hide()
            self.mouse.drag(*where, *dest)
            self.grid, self.history, self.labels = None, [], None
            return f"Dragged {n} to {to}."
        raise ToolError(f"Unknown mouse action {action}.")


def controller(ctx: ToolContext) -> GridController:
    g = ctx.services.get("grid")
    if g is None:
        cfg = ctx.settings.tools
        g = ctx.services["grid"] = GridController(cols=cfg.grid_cols, rows=cfg.grid_rows)
    return g


# --- tools ---------------------------------------------------------------------------------------
def mouse_grid(args: dict, ctx: ToolContext) -> str:
    g = controller(ctx)
    action = args["action"]
    if action == "show":
        return g.show(args.get("screen"))
    if action == "hide":
        return g.hide()
    if action == "back":
        return g.back()
    if args.get("cell") is None:
        raise ToolError("Zoom in on which number?")
    return g.zoom(int(args["cell"]))


def mouse(args: dict, ctx: ToolContext) -> str:
    return controller(ctx).act(args["action"], args.get("cell"), args.get("amount") or 3, args.get("to"))


def register(reg: ToolRegistry) -> None:
    reg.tool("mouse_grid", "Voice mouse: show a numbered grid over the screen, zoom into a numbered "
             "cell for precision, go back, or hide it. Use when the user wants to click something.",
             {"type": "object", "properties": {
                 "action": {"type": "string", "enum": ["show", "zoom", "back", "hide"]},
                 "cell": {"type": "integer", "minimum": 1, "maximum": 400},
                 "screen": {"type": "string", "description": "with show: '1', '2'..., 'next', 'left', "
                            "'right' or 'main'. Leave out for the screen the mouse is on.", "maxLength": 8}},
              "required": ["action"], "additionalProperties": False},
             risk=Risk.SAFE, category="mouse")(mouse_grid)
    reg.tool("mouse", "Click, double-click, right-click, move to, scroll or drag at a numbered grid "
             "cell (the user says the number). Without a cell: click where the mouse is.",
             {"type": "object", "properties": {
                 "action": {"type": "string", "enum": ["click", "double_click", "right_click",
                                                       "middle_click", "move", "scroll_up",
                                                       "scroll_down", "drag"]},
                 "cell": {"type": "integer", "minimum": 1, "maximum": 400},
                 "to": {"type": "integer", "minimum": 1, "maximum": 400},
                 "amount": {"type": "integer", "minimum": 1, "maximum": 30}},
              "required": ["action"], "additionalProperties": False},
             risk=Risk.SAFE, category="mouse")(mouse)
