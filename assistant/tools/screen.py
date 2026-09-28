"""Screen tools. Nova reads the screen itself first (Windows' own text recognition, tools/ocr.py:
instant, local); the picture goes to a vision model only for visual questions or when there's
hardly any text (a game, a photo)."""

from __future__ import annotations

import base64
import io
import logging
import re

from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry, ToolResult


def grab_screen(monitor: int = 1):
    """Capture a monitor as a PIL image (1 = primary, 0 = all monitors combined)."""
    import mss
    from PIL import Image

    factory = getattr(mss, "MSS", None) or mss.mss
    with factory() as sct:
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


log = logging.getLogger(__name__)
# Questions about how things look, not what's written: those need the picture.
_PICTURE = re.compile(r"\b(?:picture|photo|image|pic|look(?:s)? like|colou?rs?|logo|drawing|chart|graph|map|"
                      r"face|meme|thumbnail|design|layout|icon|video frame|who is (?:this|that))\b", re.I)


def look_at_screen(args: dict, ctx: ToolContext, _grab=None):
    if _grab is None and not args.get("picture") and not _PICTURE.search(args.get("focus") or ""):
        from assistant.tools import ocr
        try:
            where = "screen" if "monitor" in args else "window"
            label, lines = ocr.read_screen(where, int(args.get("monitor") or 1))
            if sum(len(ln.text) for ln in lines) >= 20:
                return ocr.as_text(label, lines)
        except Exception as e:                  # no Windows OCR: fall back to the picture
            log.info("reading the screen as text failed (%s); using the picture", e)
    return _picture(args, ctx, _grab)


def _picture(args: dict, ctx: ToolContext, _grab=None) -> ToolResult:
    cfg = ctx.settings.tools.screenshot
    try:
        img = (_grab or grab_screen)(args.get("monitor", 1))
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
        "See the user's screen: returns all the text in the window in front, read instantly. Use for "
        "'what's on my screen', 'read this error', 'what does it say'. Set picture to true only for "
        "how something looks (a photo, colours, a game scene).",
        {
            "type": "object",
            "properties": {
                "monitor": {"type": "integer", "minimum": 0, "maximum": 8,
                            "description": "1 = primary (default), 2+ = others, 0 = all"},
                "focus": {"type": "string", "maxLength": 300,
                          "description": "What to look for, e.g. 'the error dialog'"},
                "picture": {"type": "boolean", "description": "Describe the picture itself (slower)"},
            },
            "additionalProperties": False,
        },
        risk=Risk.SAFE, category="screen",
    )(look_at_screen)
