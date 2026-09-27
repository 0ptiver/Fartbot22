"""Nova's icon by the clock: shows its state, and a menu to open, mute, stand down, quit.

pystray runs its own thread; menu clicks are handed to the asyncio loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)

COLORS = {"idle": (70, 180, 230), "listening": (74, 222, 128), "thinking": (167, 139, 250),
          "speaking": (90, 216, 255), "confirm": (255, 181, 71), "standby": (100, 116, 139),
          "muted": (248, 113, 113), "dictation": (236, 240, 245), "starting": (100, 116, 139),
          "recording": (255, 77, 109), "enrolling": (52, 211, 153)}
LABELS = {"idle": "listening for “Nova”", "listening": "listening…", "thinking": "thinking…",
          "speaking": "speaking", "confirm": "waiting for your yes or no", "standby": "standing down",
          "muted": "microphone off", "dictation": "dictating", "starting": "starting up…",
          "recording": "watching what you do", "enrolling": "learning your voice"}


def icon_image(state: str, size: int = 64):
    """A small glowing orb in the state's colour."""
    from PIL import Image, ImageDraw, ImageFilter

    r, g, b = COLORS.get(state, COLORS["idle"])
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((6, 6, size - 6, size - 6), fill=(r, g, b, 150))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(4)))
    d = ImageDraw.Draw(img)
    d.ellipse((12, 12, size - 12, size - 12), fill=(r, g, b, 255))
    hl = size // 5
    d.ellipse((20, 18, 20 + hl, 18 + hl), fill=(255, 255, 255, 170))      # highlight
    return img


class TrayActions:
    """What the menu does. Separate from pystray so it can be tested."""

    def __init__(self, voice, aio: asyncio.AbstractEventLoop, open_window: Callable[[], None],
                 quit: Callable[[int], None]):
        self.voice, self.aio, self.open_window, self.quit = voice, aio, open_window, quit

    def _run(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self.aio)

    def toggle_mute(self) -> None:
        self.aio.call_soon_threadsafe(setattr, self.voice, "mic_muted", not self.voice.mic_muted)

    def toggle_standby(self) -> None:
        self._run(self.voice.resume() if self.voice.standby else self.voice.stand_down())

    def toggle_autostart(self) -> None:
        from assistant.background import autostart_enabled, set_autostart
        log.info(set_autostart(not autostart_enabled()))

    def open_log(self) -> None:
        from assistant.background import LOG_FILE
        from assistant.core.launch import launch
        launch(str(LOG_FILE))

    def restart(self) -> None:
        from assistant.background import EXIT_RESTART
        self.aio.call_soon_threadsafe(self.quit, EXIT_RESTART)

    def exit(self) -> None:
        from assistant.background import EXIT_QUIT
        self.aio.call_soon_threadsafe(self.quit, EXIT_QUIT)


class Tray:
    def __init__(self, actions: TrayActions, name: str = "Nova"):
        self.a, self.name = actions, name
        self.icon = None
        self.state = "starting"

    def start(self) -> None:
        try:
            import pystray
            from pystray import Menu, MenuItem as Item
        except Exception as e:                          # no tray support: Nova still works
            log.warning("tray icon unavailable: %s", e)
            return
        a = self.a

        def autostart_on(_item) -> bool:
            try:
                from assistant.background import autostart_enabled
                return autostart_enabled()
            except Exception:
                return False

        menu = Menu(
            Item(f"Open {self.name}", lambda: a.open_window(), default=True),
            Menu.SEPARATOR,
            Item("Microphone off", lambda: a.toggle_mute(), checked=lambda _i: bool(a.voice.mic_muted)),
            Item(lambda _i: "Wake up" if a.voice.standby else "Stand down", lambda: a.toggle_standby()),
            Item("Start with Windows", lambda: a.toggle_autostart(), checked=autostart_on),
            Item("Open log", lambda: a.open_log()),
            Menu.SEPARATOR,
            Item("Restart", lambda: a.restart()),
            Item(f"Quit {self.name}", lambda: a.exit()),
        )
        self.icon = pystray.Icon("nova", icon_image(self.state), f"{self.name}: {LABELS[self.state]}", menu)
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

    def set_state(self, state: str) -> None:
        if state == self.state or self.icon is None:
            self.state = state
            return
        self.state = state
        try:
            self.icon.icon = icon_image(state)
            self.icon.title = f"{self.name}: {LABELS.get(state, state)}"
        except Exception:
            log.exception("tray update failed")

    def stop(self) -> None:
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
