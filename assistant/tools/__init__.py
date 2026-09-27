"""Tool groups. Each module exposes `register(registry)`."""

from __future__ import annotations

import logging

from assistant.core.config import Settings
from assistant.tools import (browser, expert, files, grid, hudnav, keyboard, learn, memory, music, pc, quick,
                             routines, screen, system, teach, timers, uia, video, voice, watch, web)
from assistant.tools.registry import AuditLog, ToolRegistry

TOOL_MODULES = [system, screen, expert, music, files, timers, pc, grid, video, keyboard, uia, memory, watch, voice, teach,
                hudnav, learn, browser, quick]


# Hidden from the model (still run by the fast path, routines and the window). The small local
# model got worse at picking tools as the list grew to 54 (owner: "you broke him"): these are
# either said directly ("lock my PC", "show the grid", "subtitles on") or rarely needed.
MODEL_HIDDEN = {"dictation", "cancel_shutdown", "cancel_watch", "list_watches", "lessons", "set_location",
                "show_page", "subtitles", "set_voice", "voice_lock", "teach", "queue_song", "mouse", "mouse_grid",
                "show_numbers", "press_key", "now_playing", "recall", "music_control", "lock_pc", "power",
                "delete_file", "move_file"}


def build_registry(settings: Settings, audit: AuditLog | None = None) -> ToolRegistry:
    reg = ToolRegistry(settings, audit)
    for mod in TOOL_MODULES:
        mod.register(reg)
    # The Claude API brain has Claude's own web search; the local brain uses a free one.
    if settings.brain.backend == "local" and settings.brain.web_search.enabled:
        web.register(reg)
    # Last, so routine steps can be checked against every other tool.
    routines.register(reg)
    for name in MODEL_HIDDEN:
        if name in reg._tools:
            reg._tools[name].internal = True
    for problem in routines.problems(settings, reg):
        logging.getLogger(__name__).warning("routine config: %s", problem)
    return reg
