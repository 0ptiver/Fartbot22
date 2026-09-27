"""Open apps, files and links so they're fully independent of Nova's window.

os.startfile from Nova's own process makes the app a child of Nova's console, and apps
like Discord, Steam and Chrome then print their logs into it (the owner saw Discord's
log lines appear in Nova's window). So on Windows the open goes through a tiny helper
process with no console (DETACHED_PROCESS): the app finds no console to write to.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

_HELPER = "import os, sys; os.startfile(sys.argv[1])"


def _python_no_console() -> str:
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return str(pyw if pyw.exists() else exe)


def helper_command(target: str) -> list[str]:
    return [_python_no_console(), "-c", _HELPER, target]


def launch(target: str, _popen=subprocess.Popen) -> None:
    """Open `target` (a program, shortcut, document, folder, URL or app link like spotify:)."""
    if sys.platform == "win32":
        # No shell: target is passed as one argument, never parsed as a command line.
        _popen(helper_command(target), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
               stderr=subprocess.DEVNULL, close_fds=True,
               creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)
    elif sys.platform == "darwin":
        _popen(["open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        _popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)



