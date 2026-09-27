"""Tool groups. Each module exposes `register(registry)`."""

from __future__ import annotations

import logging

from assistant.core.config import Settings
from assistant.tools import (expert, files, grid, keyboard, memory, music, pc, routines, screen, system,
                             timers, uia, video, web)
from assistant.tools.registry import AuditLog, ToolRegistry

TOOL_MODULES = [system, screen, expert, music, files, timers, pc, grid, video, keyboard, uia, memory]


def build_registry(settings: Settings, audit: AuditLog | None = None) -> ToolRegistry:
    reg = ToolRegistry(settings, audit)
    for mod in TOOL_MODULES:
        mod.register(reg)
    # The Claude API brain has Claude's own web search; the local brain uses a free one.
    if settings.brain.backend == "local" and settings.brain.web_search.enabled:
        web.register(reg)
    # Last, so routine steps can be checked against every other tool.
    routines.register(reg)
    for problem in routines.problems(settings, reg):
        logging.getLogger(__name__).warning("routine config: %s", problem)
    return reg
