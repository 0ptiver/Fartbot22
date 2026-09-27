"""Plugin-style tool registry with JSON-schema validation, risk policy and audit logging."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable

import jsonschema

from assistant.core.config import Settings


class Risk(StrEnum):
    SAFE = "safe"          # runs immediately
    CONFIRM = "confirm"    # needs an explicit yes (voice or UI)
    BLOCKED = "blocked"    # never runs


ConfirmFn = Callable[[str, dict[str, Any]], Awaitable[bool]]


@dataclass
class ToolContext:
    settings: Settings
    client_id: str = "local"
    remote: bool = False
    confirm: ConfirmFn | None = None
    services: dict[str, Any] = field(default_factory=dict)  # e.g. {"brain": Brain}


@dataclass
class ToolResult:
    # A string, or a list of Claude content blocks (text / image) for rich results.
    content: str | list[dict[str, Any]]
    is_error: bool = False


Handler = Callable[[dict[str, Any], ToolContext], Any]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: Risk
    handler: Handler
    # Sync handlers run in a worker thread so they never block the event loop.
    category: str = "general"
    # For confirmations: args -> what will happen, e.g. "put the PC to sleep".
    describe: Callable[[dict[str, Any]], str] | None = None

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "eager_input_streaming": True,
        }


class ToolError(Exception):
    """Raised by handlers for expected failures; the message is shown to the model."""


class AuditLog:
    """Append-only JSONL log of every tool call."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _write(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def write(self, record: dict[str, Any]) -> None:
        await asyncio.to_thread(self._write, record)

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(line) for line in lines if line.strip()]


def _summarize_content(content: str | list[dict[str, Any]], limit: int = 500) -> str:
    if isinstance(content, str):
        return content[:limit]
    parts = []
    for block in content:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        else:
            parts.append(f"[{block.get('type')}]")
    return " ".join(parts)[:limit]


class ToolRegistry:
    def __init__(self, settings: Settings, audit: AuditLog | None = None):
        self.settings = settings
        self.audit = audit or AuditLog(settings.safety.audit_path())
        self._tools: dict[str, Tool] = {}
        # Called with (name, args, ok) after each tool runs (e.g. a lesson being recorded).
        self.observers: list[Callable[[str, Any, bool], None]] = []

    # --- registration -------------------------------------------------------
    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        jsonschema.Draft202012Validator.check_schema(tool.input_schema)
        self._tools[tool.name] = tool
        return tool

    def tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any] | None = None,
        risk: Risk = Risk.SAFE,
        category: str = "general",
        describe: Callable[[dict[str, Any]], str] | None = None,
    ) -> Callable[[Handler], Handler]:
        schema = input_schema or {"type": "object", "properties": {}}

        def deco(fn: Handler) -> Handler:
            self.register(Tool(name, description, schema, risk, fn, category, describe))
            return fn

        return deco

    def describe(self, name: str, args: dict[str, Any]) -> str:
        """Plain-words description of a pending action, for confirmation prompts."""
        tool = self._tools.get(name)
        if tool is not None and tool.describe is not None:
            try:
                return tool.describe(args)
            except Exception:
                pass
        detail = ", ".join(f"{k} {v}" for k, v in args.items() if isinstance(v, (str, int, float)))
        return name.replace("_", " ") + (f" ({detail})" if detail else "")

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def definitions(self) -> list[dict[str, Any]]:
        # Deterministic order keeps the prompt-cache prefix stable.
        return [self._tools[n].definition() for n in self.names()
                if self.effective_risk(n, remote=False) != Risk.BLOCKED]

    # --- policy -------------------------------------------------------------
    def effective_risk(self, name: str, remote: bool) -> Risk:
        tool = self._tools[name]
        override = self.settings.safety.risk_overrides.get(name)
        risk = Risk(override) if override else tool.risk
        if remote and name in self.settings.safety.remote_blocked_tools:
            return Risk.BLOCKED
        return risk

    # --- execution ----------------------------------------------------------
    async def execute(self, name: str, args: Any, ctx: ToolContext) -> ToolResult:
        started = time.time()
        record: dict[str, Any] = {
            "ts": started,
            "client": ctx.client_id,
            "remote": ctx.remote,
            "tool": name,
            "args": args,
        }
        try:
            result = await self._execute(name, args, ctx, record)
        except asyncio.CancelledError:
            record["duration_ms"] = round((time.time() - started) * 1000)
            self.audit._write(record)  # sync: we are unwinding a cancellation
            raise
        record["duration_ms"] = round((time.time() - started) * 1000)
        record["is_error"] = result.is_error
        record["result"] = _summarize_content(result.content)
        await self.audit.write(record)
        for fn in list(self.observers):
            try:
                fn(name, args, not result.is_error)
            except Exception:
                pass
        return result

    async def _execute(self, name: str, args: Any, ctx: ToolContext, record: dict) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            record["status"] = "unknown_tool"
            return ToolResult(f"Unknown tool: {name}", is_error=True)
        if not isinstance(args, dict):
            record["status"] = "invalid_args"
            return ToolResult(json.dumps({"INVALID_JSON": json.dumps(args)}), is_error=True)
        try:
            jsonschema.validate(args, tool.input_schema)
        except jsonschema.ValidationError as e:
            record["status"] = "invalid_args"
            return ToolResult(f"Invalid arguments: {e.message}", is_error=True)

        risk = self.effective_risk(name, ctx.remote)
        record["risk"] = risk.value
        if risk == Risk.BLOCKED:
            record["status"] = "blocked"
            where = "from a remote device" if ctx.remote else "by policy"
            return ToolResult(f"The tool {name} is blocked {where}.", is_error=True)
        if risk == Risk.CONFIRM:
            approved = bool(ctx.confirm and await ctx.confirm(name, args))
            record["confirmed"] = approved
            if not approved:
                record["status"] = "declined"
                return ToolResult("The user declined this action.", is_error=True)

        try:
            if inspect.iscoroutinefunction(tool.handler):
                out = await tool.handler(args, ctx)
            else:
                out = await asyncio.to_thread(tool.handler, args, ctx)
        except asyncio.CancelledError:
            record["status"] = "cancelled"
            raise
        except ToolError as e:
            record["status"] = "error"
            return ToolResult(str(e), is_error=True)
        except Exception as e:  # unexpected failure: report, don't crash the session
            record["status"] = "exception"
            return ToolResult(f"{type(e).__name__}: {e}", is_error=True)

        record["status"] = "ok"
        if isinstance(out, ToolResult):
            return out
        if isinstance(out, (str, list)):
            return ToolResult(out)
        return ToolResult(json.dumps(out, default=str))
