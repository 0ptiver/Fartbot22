"""Global push-to-talk hotkey (hold to talk). Uses the `keyboard` package on Windows."""

from __future__ import annotations

import threading
import time


class PushToTalk:
    def __init__(self, combo: str):
        self.combo = combo
        self.held = threading.Event()
        self._watching = False

    def start(self) -> None:
        import keyboard

        def on_press() -> None:
            if self._watching:
                return
            self._watching = True
            self.held.set()
            threading.Thread(target=self._watch_release, daemon=True).start()

        keyboard.add_hotkey(self.combo, on_press, suppress=False, trigger_on_release=False)

    def _watch_release(self) -> None:
        import keyboard

        try:
            while keyboard.is_pressed(self.combo):
                time.sleep(0.015)
        finally:
            self.held.clear()
            self._watching = False

    def stop(self) -> None:
        import keyboard

        keyboard.unhook_all_hotkeys()


class NoHotkey:
    """Stand-in when push-to-talk isn't used (open mic, tests)."""

    held = threading.Event()

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass
