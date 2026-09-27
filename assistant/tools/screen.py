"""Screen tools: capture the screen and hand it to Claude's vision."""

from __future__ import annotations

import base64
import io

from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry, ToolResult


def grab_screen(monitor: int = 1):
    """Capture a monitor as a PIL image (1 = primary, 0 = all monitors combined)."""
    import mss
    from PIL import Image

    with mss.mss() as sct:
        if monitor >= len(sct.monitors):
            raise ToolError(f"There is no monitor {monitor}; I can see {len(sct.monitors) - 1}.")
        shot = sct.grab(sct.monitors[monitor])
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def encode_image(img, max_edge: int, quality: int) -> str:
    img = img.copy()
    img.thumbnail((max_edge, max_edge))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def look_at_screen(args: dict, ctx: ToolContext, _grab=grab_screen) -> ToolResult:
    cfg = ctx.settings.tools.screenshot
    try:
        img = _grab(args.get("monitor", 1))
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Screen capture failed: {e}") from e
    data = encode_image(img, cfg.max_edge_px, cfg.jpeg_quality)
    return ToolResult([
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
        {"type": "text", "text": f"Screenshot of monitor {args.get('monitor', 1)}. "
                                 f"Focus on: {args.get('focus') or 'whatever the user asked about'}."},
    ])


def register(reg: ToolRegistry) -> None:
    reg.tool(
        "look_at_screen",
        "Take a screenshot of the user's screen so you can see it. Use for 'what's on my "
        "screen', 'read this error', 'what am I looking at'.",
        {
            "type": "object",
            "properties": {
                "monitor": {"type": "integer", "minimum": 0, "maximum": 8,
                            "description": "1 = primary (default), 2+ = others, 0 = all"},
                "focus": {"type": "string", "maxLength": 300,
                          "description": "What to look for, e.g. 'the error dialog'"},
            },
            "additionalProperties": False,
        },
        risk=Risk.SAFE, category="screen",
    )(look_at_screen)
