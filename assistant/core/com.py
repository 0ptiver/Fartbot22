"""Windows COM, set up once per thread, the same way everywhere.

Tools run on a shared pool of worker threads. Click-by-name (UI Automation) set its thread to
multithreaded COM while the volume control asked for single-threaded, so whichever ran second on
the same thread failed with "Cannot change thread mode after it is set" (owner's case: "turn up
the volume" -> OSError [WinError -2147417850]). Every part of Nova now asks for multithreaded, and
a thread that already has a mode is simply used as it is: COM works either way for these calls.
"""

from __future__ import annotations

import sys

RPC_E_CHANGED_MODE = -2147417850          # 0x80010106: this thread already has the other mode


def com_ready() -> None:
    if sys.platform != "win32":
        return
    import comtypes
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError as e:
        if getattr(e, "winerror", None) != RPC_E_CHANGED_MODE and RPC_E_CHANGED_MODE not in e.args:
            raise
