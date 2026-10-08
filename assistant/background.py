"""Always-on Nova: start with Windows, live in the tray, restart after a crash, log to a file.

    python -m assistant background        the supervisor (what Windows starts at sign-in)
      -> pythonw -m assistant voice --background     the real Nova, with the tray icon
    python -m assistant autostart on|off|status

The supervisor is tiny and has no UI: it starts Nova, and starts it again if it crashes
(or couldn't start yet, e.g. Ollama still loading after boot), waiting a little longer each
time. Quitting from the tray ends both. Restart from the tray starts a fresh Nova.
"""

from __future__ import annotations

import io
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from assistant.core.config import ROOT

LOG_DIR = ROOT / "data" / "logs"
LOG_FILE = LOG_DIR / "nova.log"
EXIT_QUIT, EXIT_RESTART = 0, 3
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Nova"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

log = logging.getLogger("nova.background")


# --- logging instead of a console ------------------------------------------------------------------
class _LogStream(io.TextIOBase):
    """Stands in for stdout/stderr under pythonw (where they're None): whole lines go to the
    log file, without the colour codes."""

    def __init__(self, logger: logging.Logger, level: int):
        super().__init__()
        self.logger, self.level, self._buf = logger, level, ""

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        self._buf += _ANSI.sub("", s)
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.logger.log(self.level, line.rstrip())
        return len(s)

    def flush(self) -> None:
        if self._buf.strip():
            self.logger.log(self.level, self._buf.rstrip())
        self._buf = ""


def log_to_file(path: Path = LOG_FILE, max_bytes: int = 1_000_000, backups: int = 3) -> None:
    """Everything (print output, warnings, errors) goes to data/logs/nova.log, which never
    grows past ~4 MB (it rolls over to nova.log.1 ... .3)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore", "uvicorn", "uvicorn.access", "asyncio", "comtypes", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    console = logging.getLogger("nova.console")
    sys.stdout = _LogStream(console, logging.INFO)
    sys.stderr = _LogStream(console, logging.WARNING)


# --- one Nova at a time -------------------------------------------------------------------------
_mutex_handle = None


def claim_single_instance(name: str = "Local\\NovaAssistant") -> bool:
    """True if this is the only Nova running. (Windows named mutex; a lock file elsewhere.)"""
    global _mutex_handle
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        _mutex_handle = k32.CreateMutexW(None, False, name)
        return k32.GetLastError() != 183                   # ERROR_ALREADY_EXISTS
    import fcntl
    lock = ROOT / "data" / "nova.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    _mutex_handle = open(lock, "w")
    try:
        fcntl.flock(_mutex_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


# --- start with Windows ---------------------------------------------------------------------------
def pythonw() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def autostart_command() -> str:
    return f'"{pythonw()}" -m assistant background'


def _winreg():
    import winreg
    return winreg


def autostart_enabled(_reg=None) -> bool:
    reg = _reg or _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = reg.QueryValueEx(key, RUN_NAME)
            return bool(value)
    except OSError:
        return False


def set_autostart(on: bool, _reg=None) -> str:
    """Your own sign-in only (HKEY_CURRENT_USER): no admin rights, easy to undo."""
    reg = _reg or _winreg()
    with reg.CreateKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if on:
            reg.SetValueEx(key, RUN_NAME, 0, reg.REG_SZ, autostart_command())
            return "Nova will start by itself when you sign in to Windows."
        try:
            reg.DeleteValue(key, RUN_NAME)
        except OSError:
            pass
        return "Nova won't start with Windows any more."


# --- the supervisor ---------------------------------------------------------------------------------
def child_command() -> list[str]:
    return [pythonw(), "-m", "assistant", "voice", "--background"]


def supervise(spawn: Callable[[], int] | None = None, sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic, max_runs: int | None = None) -> int:
    """Run Nova; start it again after a crash, waiting 5 s, 10 s, 20 s ... (at most a minute).
    A run that lasted over 10 minutes resets the wait. Quit ends; Restart starts again at once."""
    if spawn is None:
        def spawn() -> int:
            flags = 0x08000000 if sys.platform == "win32" else 0        # CREATE_NO_WINDOW
            return subprocess.run(child_command(), cwd=ROOT, creationflags=flags).returncode
    delay, runs = 5.0, 0
    while max_runs is None or runs < max_runs:
        runs += 1
        started = clock()
        code = spawn()
        if code == EXIT_QUIT:
            log.info("Nova quit")
            return 0
        if code == EXIT_RESTART:
            log.info("restarting Nova")
            delay = 5.0
            continue
        if clock() - started > 600:
            delay = 5.0
        log.warning("Nova stopped (exit code %s); starting it again in %.0f s", code, delay)
        sleep(delay)
        delay = min(delay * 2, 60.0)
    return 1


def main_supervisor() -> None:
    if sys.stdout is not None and sys.stdout.isatty():
        # Started by hand from PowerShell: the window goes quiet from here on, which looks frozen.
        print("Nova is starting in the tray (look for the dot near the clock).\n"
              "This window stays blank while Nova runs: closing it stops Nova.\n"
              "To start Nova without a window: .venv\\Scripts\\pythonw -m assistant background\n"
              f"Nova's messages go to {LOG_FILE}", flush=True)
    log_to_file(LOG_DIR / "supervisor.log", max_bytes=200_000, backups=1)
    os.chdir(ROOT)
    sys.exit(supervise())
