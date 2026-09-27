"""Configuration loading: config/config.yaml -> typed settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "config" / "config.yaml"


class AssistantConfig(BaseModel):
    name: str = "Orion"
    wake_phrase: str = "hey orion"
    address_user_as: str = "sir"
    timezone: str = "America/Chicago"
    personality: str = "british_butler"


class WebSearchConfig(BaseModel):
    enabled: bool = True
    max_uses: int = 3


class BrainConfig(BaseModel):
    chat_model: str = "claude-haiku-4-5"
    expert_model: str = "claude-opus-5"
    chat_max_tokens: int = 1024
    expert_max_tokens: int = 16000
    max_tool_rounds: int = 8
    history_turns: int = 20
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class ScreenshotConfig(BaseModel):
    max_edge_px: int = 1568
    jpeg_quality: int = 80


class ToolsConfig(BaseModel):
    app_aliases: dict[str, str] = Field(default_factory=dict)
    screenshot: ScreenshotConfig = Field(default_factory=ScreenshotConfig)


class SafetyConfig(BaseModel):
    risk_overrides: dict[str, str] = Field(default_factory=dict)
    remote_blocked_tools: list[str] = Field(default_factory=list)
    audit_log: str = "data/audit.jsonl"

    def audit_path(self) -> Path:
        p = Path(self.audit_log)
        return p if p.is_absolute() else ROOT / p


class Settings(BaseModel):
    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)


def load_settings(path: str | Path | None = None) -> Settings:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(data)
