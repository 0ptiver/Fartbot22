"""Nova's hands, lent to Claude: a small MCP server (JSON-RPC over stdin/stdout) that Claude Code
starts when Nova hands it a task it couldn't do itself. Owner: "you need to give it all of the
tools for it to do anything on the computer and think and navigate when something goes wrong".

Only Nova's own SAFE tools are offered (the browser, any app, windows, video, music, typing,
clicking...), with every check they already have: addresses are fact-checked, typing into a
terminal is refused (nobody is there to approve it), and nothing that asks first (shutting down,
deleting, moving files) exists here at all. Plus `screenshot` so Claude can see, and `screen_click`
to click a spot on that screenshot (for things apps don't describe, like games).
Every call is written to the audit log and to the run log Nova reads (live feed + learning).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

PROTOCOL = "2025-06-18"
# Nova's tools Claude may use (only if they're SAFE in this configuration).
AGENT_TOOLS = ["my_browser", "app", "window", "open_app", "open_website", "video", "media", "volume",
               "brightness", "click_element", "press_keys", "type_text", "play_music", "music_control",
               "now_playing", "system_status", "get_time", "find_files", "open_file", "read_file",
               "weather", "calculate", "convert"]
# Things that only look: not worth learning as steps.
LOOK_ONLY = {"screenshot", "now_playing", "system_status", "get_time", "find_files", "read_file", "weather",
             "calculate", "convert", "summarize_page"}


class _NoOverlay:
    """The grid's on-screen overlay isn't needed to click for Claude."""

    def hide(self):
        pass

    def show(self, *a, **k):
        pass


class NovaTools:
    def __init__(self, settings=None, registry=None, log_path: str | None = None):
        from assistant.core.config import load_settings
        from assistant.tools import build_registry
        from assistant.tools.grid import GridController
        from assistant.tools.registry import AuditLog, Risk, ToolContext
        self.settings = settings or load_settings()
        self.registry = registry or build_registry(self.settings, AuditLog(self.settings.safety.audit_path()))
        self.ctx = ToolContext(self.settings, client_id="claude-agent",
                               services={"grid": GridController(overlay=_NoOverlay())})
        self.names = [n for n in AGENT_TOOLS if n in self.registry._tools
                      and self.registry.effective_risk(n, remote=False) == Risk.SAFE]
        self.log_path = log_path or os.environ.get("NOVA_AGENT_LOG")
        self.shot: dict | None = None          # geometry of the last screenshot, for screen_click

    # --- the tool list --------------------------------------------------------------------------
    def list(self) -> list[dict]:
        out = []
        for name in self.names:
            d = self.registry._tools[name].definition()
            out.append({"name": d["name"], "description": d["description"], "inputSchema": d["input_schema"]})
        out.append({"name": "screenshot", "description": "See a screen (monitor 1 = main, 2 = second). Returns the image. "
                    "Use it to check what happened after each step.",
                    "inputSchema": {"type": "object", "properties": {"monitor": {"type": "integer", "minimum": 1,
                                                                                 "maximum": 4}}}})
        out.append({"name": "screen_click", "description": "Click a spot on the last screenshot, in that image's "
                    "pixels (x from the left, y from the top). For things an app doesn't name (games, pictures). "
                    "Prefer app/click_element by name when possible.",
                    "inputSchema": {"type": "object", "properties": {
                        "x": {"type": "integer", "minimum": 0}, "y": {"type": "integer", "minimum": 0},
                        "button": {"type": "string", "enum": ["left", "right", "double"]}},
                        "required": ["x", "y"]}})
        return out

    # --- calling -------------------------------------------------------------------------------
    async def call(self, name: str, args: dict) -> tuple[list[dict], bool]:
        started = time.time()
        try:
            if name == "screenshot":
                content, ok = await asyncio.to_thread(self._screenshot, int(args.get("monitor") or 1)), True
            elif name == "screen_click":
                content, ok = [{"type": "text", "text": await asyncio.to_thread(self._screen_click, args)}], True
            elif name in self.names:
                res = await self.registry.execute(name, args, self.ctx)
                content, ok = _content(res.content), not res.is_error
            else:
                content, ok = [{"type": "text", "text": f"There's no tool called {name}."}], False
        except Exception as e:                  # a tool must never crash the server
            content, ok = [{"type": "text", "text": f"{type(e).__name__}: {e}"}], False
        self._log({"tool": name, "args": args, "ok": ok, "ms": round((time.time() - started) * 1000),
                   "result": next((c["text"][:200] for c in content if c["type"] == "text"), "")})
        return content, ok

    def _screenshot(self, monitor: int) -> list[dict]:
        from assistant.tools.grid import _dpi_aware
        from assistant.tools.screen import encode_image, grab_screen
        _dpi_aware()
        import mss
        factory = getattr(mss, "MSS", None) or mss.mss
        with factory() as sct:
            mons = sct.monitors
            if monitor >= len(mons):
                return [{"type": "text", "text": f"There's no monitor {monitor}; there are {len(mons) - 1}."}]
            m = mons[monitor]
        img = grab_screen(monitor)
        cfg = self.settings.tools.screenshot
        data = encode_image(img, cfg.max_edge_px, cfg.jpeg_quality)
        scale = min(1.0, cfg.max_edge_px / max(img.size))
        self.shot = {"left": m["left"], "top": m["top"], "scale": scale,
                     "w": round(img.size[0] * scale), "h": round(img.size[1] * scale)}
        return [{"type": "image", "data": data, "mimeType": "image/jpeg"},
                {"type": "text", "text": f"Monitor {monitor}, {self.shot['w']}x{self.shot['h']} pixels as shown."}]

    def _screen_click(self, args: dict) -> str:
        if not self.shot:
            return "Take a screenshot first, then click on it."
        x, y = int(args["x"]), int(args["y"])
        if not (0 <= x <= self.shot["w"] and 0 <= y <= self.shot["h"]):
            return "That spot is outside the screenshot."
        g = self.ctx.services["grid"]
        px = self.shot["left"] + round(x / self.shot["scale"])
        py = self.shot["top"] + round(y / self.shot["scale"])
        g.mouse.move(px, py)
        time.sleep(0.05)
        button = args.get("button") or "left"
        g.mouse.click("right" if button == "right" else "left", button == "double")
        return f"Clicked at {x},{y}."

    def _log(self, entry: dict) -> None:
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": time.time(), **entry}) + "\n")
        except OSError:
            pass


def _content(content: Any) -> list[dict]:
    """Nova's tool results (text, or Claude-API-style blocks) as MCP content."""
    if isinstance(content, str):
        return [{"type": "text", "text": content or "Done."}]
    out = []
    for b in content or []:
        if b.get("type") == "text":
            out.append({"type": "text", "text": b["text"]})
        elif b.get("type") == "image":
            src = b.get("source", {})
            out.append({"type": "image", "data": src.get("data", ""), "mimeType": src.get("media_type", "image/jpeg")})
    return out or [{"type": "text", "text": "Done."}]


# --- JSON-RPC over stdio --------------------------------------------------------------------------
async def handle(tools: NovaTools, msg: dict) -> dict | None:
    """One JSON-RPC message in, the reply out (None for notifications)."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None                              # notifications/initialized etc.
    try:
        if method == "initialize":
            asked = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
            result = {"protocolVersion": asked, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "nova", "version": "1"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tools.list()}
        elif method == "tools/call":
            p = msg.get("params") or {}
            content, ok = await tools.call(p.get("name", ""), p.get("arguments") or {})
            result = {"content": content, "isError": not ok}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)[:300]}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    # stdout carries the protocol only: anything else a library prints goes to stderr.
    out = sys.stdout
    sys.stdout = sys.stderr
    tools = NovaTools()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            reply = await handle(tools, msg)
            if reply is not None:
                out.write(json.dumps(reply) + "\n")
                out.flush()
    asyncio.run(run())


if __name__ == "__main__":
    main()
