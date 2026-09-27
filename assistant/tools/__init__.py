"""Tool groups. Each module exposes `register(registry)`."""

from __future__ import annotations

from assistant.core.config import Settings
from assistant.tools import expert, screen, system
from assistant.tools.registry import AuditLog, ToolRegistry

TOOL_MODULES = [system, screen, expert]


def build_registry(settings: Settings, audit: AuditLog | None = None) -> ToolRegistry:
    reg = ToolRegistry(settings, audit)
    for mod in TOOL_MODULES:
        mod.register(reg)
    return reg
