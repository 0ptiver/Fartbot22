"""'Change your voice to deep butler': switch to a voice preset (the HUD's Voice tab designs new ones)."""

from __future__ import annotations

from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry
from assistant.voice.voicedesign import PRESETS, VoiceDesign


def _preset(name: str) -> tuple[str, dict] | None:
    want = name.lower().replace("voice", "").strip(" .")
    if want in ("normal", "default", "usual", "original", "old", "regular", "standard"):
        want = "butler"
    for key, value in PRESETS.items():
        if key.lower() == want or want and want in key.lower():
            return key, value
    return None


async def set_voice(args: dict, ctx: ToolContext) -> str:
    loop = ctx.services.get("voice")
    if loop is None:
        raise ToolError("I can only change my voice while we're talking.")
    found = _preset(args["preset"])
    if found is None:
        raise ToolError("I have these voices: " + ", ".join(PRESETS) + ". Or design one in my window's Voice tab.")
    key, value = found
    try:
        await loop.set_voice(VoiceDesign.from_dict(value))
    except RuntimeError as e:
        raise ToolError(str(e)) from e
    return f"Voice changed to {key}. How's this?"


def register(reg: ToolRegistry) -> None:
    register_subtitles(reg)
    reg.tool("set_voice", "Change your speaking voice to a preset: " + ", ".join(PRESETS) + ".",
             {"type": "object", "properties": {"preset": {"type": "string", "maxLength": 40}},
              "required": ["preset"], "additionalProperties": False}, risk=Risk.SAFE, category="voice")(set_voice)


def voice_intent(t: str) -> tuple[str, dict] | None:
    import re
    m = re.fullmatch(r"(?:change|switch|set|make) (?:your|the) voice (?:to|into) (?:the |a |an )?(.+?)(?: voice| one)?"
                     r"|use (?:the |your )?(.+?) voice", t)
    if m:
        return "set_voice", {"preset": m.group(1) or m.group(2)}
    return None


# --- live subtitles ---------------------------------------------------------------------------------
async def subtitles(args: dict, ctx: ToolContext) -> str:
    subs = ctx.services.get("subtitles")
    if subs is None:
        raise ToolError("Subtitles only work while I'm running in voice mode.")
    if not args["on"]:
        if not subs.on:
            return "Subtitles are already off."
        await subs.stop()
        return "Subtitles off."
    translate = args.get("translate", True)
    try:
        await subs.start(translate=translate)
    except RuntimeError as e:
        raise ToolError(str(e)) from e
    return ("Subtitles on" + (", translating into English" if translate else "") +
            ". They show near the bottom of your screen.")


def register_subtitles(reg: ToolRegistry) -> None:
    reg.tool("subtitles", "Live captions of what the PC is playing (game voice chat, videos), shown on "
             "screen and translated into English when it's another language.",
             {"type": "object", "properties": {"on": {"type": "boolean"}, "translate": {"type": "boolean"}},
              "required": ["on"], "additionalProperties": False}, risk=Risk.SAFE, category="voice")(subtitles)


def subtitles_intent(t: str) -> tuple[str, dict] | None:
    import re
    words = r"(?:live )?(?:subtitles|captions|subs|cc|closed captions)"
    if re.fullmatch(r"(?:turn |switch |put )?(?:on )?(?:the )?" + words + r"(?: on)?|(?:turn|switch|put) on (?:the )?" + words
                    + r"|(?:show|give me) (?:the )?" + words + r"|caption (?:this|that|it|the game|the video)", t):
        return "subtitles", {"on": True}
    if re.fullmatch(r"(?:translate|translate for me|what are they saying|what is he saying|what is she saying)"
                    r"(?: (?:this|that|it|the game|the video|them|what they're saying|what they are saying))?", t):
        return "subtitles", {"on": True, "translate": True}
    if re.fullmatch(r"(?:turn |switch )?(?:off )?(?:the )?" + words + r" off|(?:turn|switch) off (?:the )?" + words
                    + r"|(?:hide|stop|close) (?:the )?" + words + r"|stop translating", t):
        return "subtitles", {"on": False}
    return None
