"""Tool groups. Each module exposes `register(registry)`."""

from __future__ import annotations

from assistant.core.config import Settings
from assistant.tools import expert, music, screen, system, web
from assistant.tools.registry import AuditLog, ToolRegistry

TOOL_MODULES = [system, screen, expert, music]


def build_registry(settings: Settings, audit: AuditLog | None = None) -> ToolRegistry:
    reg = ToolRegistry(settings, audit)
    for mod in TOOL_MODULES:
        mod.register(reg)
    # The Claude API brain has Claude's own web search; the local brain uses a free one.
    if settings.brain.backend == "local" and settings.brain.web_search.enabled:
        web.register(reg)
    return reg
