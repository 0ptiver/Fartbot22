"""Make pip-installed NVIDIA CUDA/cuDNN DLLs findable on Windows (no CUDA toolkit needed)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_done = False


def add_nvidia_dll_dirs() -> list[str]:
    global _done
    if _done or sys.platform != "win32":
        return []
    _done = True
    added = []
    for entry in sys.path:
        root = Path(entry) / "nvidia"
        if not root.is_dir():
            continue
        for bin_dir in root.glob("*/bin"):
            os.add_dll_directory(str(bin_dir))  # type: ignore[attr-defined]
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            added.append(str(bin_dir))
    return added
