"""File-access policy: Nova may only touch files inside the owner's chosen folders.

- Paths are fully resolved first (".." tricks, symlinks and shortcuts can't escape).
- Hidden folders (.ssh, .git, ...), AppData and anything that looks like a secret
  (passwords, keys, wallets, browser profiles) are always refused, even inside an
  allowed folder.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_FOLDERS = ["~/Desktop", "~/Documents", "~/Downloads", "~/Music", "~/Pictures", "~/Videos"]


def documents_folder() -> Path:
    """The real Documents folder (Windows often moves it into OneDrive), else ~/Documents."""
    import sys
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]
            docs = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(docs), 0, None, ctypes.byref(out)) == 0:
                path = Path(out.value)
                ctypes.windll.ole32.CoTaskMemFree(out)
                return path
        except Exception:
            pass
    return Path.home() / "Documents"

_SECRET_NAME = re.compile(
    r"(passw|secret|credential|token|private[-_ ]?key|id_rsa|id_ed25519|wallet|seed[-_ ]?phrase"
    r"|recovery[-_ ]?code|2fa|otp|\.kdbx$|\.pem$|\.key$|\.pfx$|\.p12$|\.ppk$|\.env$|keychain)", re.I)
_BLOCKED_PARTS = {"appdata", "application data", "$recycle.bin", "system volume information"}
# Opening these runs code, so it needs a confirmation.
EXECUTABLE_EXT = {".exe", ".bat", ".cmd", ".com", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse",
                  ".wsf", ".wsh", ".msi", ".msp", ".scr", ".pif", ".reg", ".lnk", ".url", ".jar",
                  ".py", ".pyw", ".hta", ".cpl", ".dll", ".sys",
                  # Also run code or settings when opened on Windows, or mount a disk image whose
                  # files skip the "downloaded from the internet" warning.
                  ".msc", ".appref-ms", ".application", ".settingcontent-ms", ".search-ms", ".library-ms",
                  ".chm", ".scf", ".inf", ".ins", ".isp", ".xll", ".xlam", ".ppam", ".gadget", ".diagcab",
                  ".ws", ".wsc", ".sct", ".vb", ".psd1", ".ps1xml", ".psc1", ".mst", ".msix", ".msixbundle",
                  ".appx", ".appxbundle", ".ocx", ".job", ".mde", ".ade", ".adp", ".crt", ".der",
                  ".iso", ".img", ".vhd", ".vhdx", ".theme", ".themepack", ".desktopthemepack",
                  ".website", ".jnlp", ".cab"}


class PathNotAllowed(Exception):
    pass


def allowed_roots(folders: list[str] | None = None) -> list[Path]:
    """The allowed folders that exist, including OneDrive-redirected copies on Windows."""
    roots = []
    for f in folders or DEFAULT_FOLDERS:
        p = Path(os.path.expandvars(os.path.expanduser(f)))
        candidates = [p]
        if f.startswith("~/"):
            candidates.append(Path.home() / "OneDrive" / f[2:])
        for c in candidates:
            if c.exists():
                roots.append(c.resolve())
    return list(dict.fromkeys(roots))


def check_path(path: str | Path, roots: list[Path], must_exist: bool = True) -> Path:
    """Resolve `path` and make sure it's inside an allowed folder and not sensitive."""
    raw = Path(os.path.expandvars(os.path.expanduser(str(path).strip().strip('"'))))
    if not raw.is_absolute():
        raise PathNotAllowed("Give me a full path, or ask me to find the file first.")
    resolved = raw.resolve()
    if must_exist and not resolved.exists():
        raise PathNotAllowed(f"I can't find {raw}.")
    if not any(resolved == r or resolved.is_relative_to(r) for r in roots):
        raise PathNotAllowed("That's outside the folders I'm allowed to use "
                             "(Desktop, Documents, Downloads, Music, Pictures, Videos).")
    for root in roots:
        if resolved.is_relative_to(root):
            inner = resolved.relative_to(root).parts
            break
    else:
        inner = ()
    for part in inner:
        if part.startswith(".") or part.lower() in _BLOCKED_PARTS:
            raise PathNotAllowed("That's in a hidden or system folder, which I don't touch.")
    if _SECRET_NAME.search(resolved.name):
        raise PathNotAllowed("That looks like a password or key file, so I won't touch it.")
    return resolved


def is_sensitive(path: Path) -> bool:
    return bool(_SECRET_NAME.search(path.name)) or any(
        p.startswith(".") or p.lower() in _BLOCKED_PARTS for p in path.parts[1:])
