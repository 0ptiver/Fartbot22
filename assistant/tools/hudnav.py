"""Nova drives its own window: "show me my timers", "open your brain", "back to the dashboard"."""

from __future__ import annotations

import re

from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry

PAGES = {"home": "the dashboard", "chat": "the chat", "activity": "the activity log", "timers": "your timers",
         "routines": "your routines", "brain": "my memory", "voice": "the voice settings", "phone": "phone access"}
_WORDS = {
    "home": r"(?:the |your )?(?:dashboard|home(?: screen| page)?|main page)",
    "chat": r"(?:the |our )?(?:chat|conversation)",
    "activity": r"(?:the |your |my )?(?:activity|activity log|log|history of what you did)",
    "timers": r"(?:the |my )?(?:timers?|reminders?|alarms?|watches)",
    "routines": r"(?:the |my |your )?routines?",
    "brain": r"(?:the |your )?(?:brain|memory|memories)|what you remember",
    "voice": r"(?:the |your )?voice(?: settings| page)?|voice lock settings",
    "phone": r"(?:the )?phone(?: access| page| settings)?",
}


def page_intent(t: str) -> tuple[str, dict] | None:
    """'show me my timers', 'open your brain', 'go to the dashboard', 'back to the chat'."""
    for page, words in _WORDS.items():
        if re.fullmatch(r"(?:show(?: me)?|open(?: up)?|go (?:back )?to|back to|switch to|bring up|pull up|take me to)"
                        r" (?:" + words + r")(?: (?:page|tab|screen|view))?(?: in the (?:window|hud))?", t):
            return "show_page", {"page": page}
    return None


async def show_page(args: dict, ctx: ToolContext) -> str:
    page = args.get("page")
    if page not in PAGES:
        raise ToolError("I don't have a page for that.")
    loop = ctx.services.get("voice")
    if loop is None:
        raise ToolError("My window only follows along when I'm running with my voice.")
    loop.on_event({"type": "navigate", "page": page})
    return f"Here's {PAGES[page]}."


def register(reg: ToolRegistry) -> None:
    reg.tool("show_page", "Switch Nova's own window (the HUD) to a page: home (dashboard), chat, activity, "
             "timers, routines, brain (memories), voice, phone.",
             {"type": "object", "properties": {"page": {"type": "string", "enum": list(PAGES)}},
              "required": ["page"], "additionalProperties": False},
             risk=Risk.SAFE, category="system")(show_page)
