"""Brains: `create_brain` picks the everyday model from config."""

from __future__ import annotations

from assistant.core.config import Settings
from assistant.tools.registry import ToolRegistry


def create_brain(settings: Settings, registry: ToolRegistry | None = None):
    if registry is None:
        from assistant.tools import build_registry
        registry = build_registry(settings)
    backend = settings.brain.backend
    if backend == "local":
        from assistant.brain.local import LocalBrain
        return LocalBrain(settings, registry)
    if backend == "anthropic":
        from assistant.brain.llm import Brain
        return Brain(settings, registry)
    raise ValueError(f"unknown brain backend: {backend}")
