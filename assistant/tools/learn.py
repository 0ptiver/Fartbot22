"""What Nova has learned from corrections and teaching: list it, forget it (by voice)."""

from __future__ import annotations

import re

from assistant.tools.registry import Risk, ToolContext, ToolRegistry

_LIST = re.compile(r"^(?:what (?:have|did) you (?:learn(?:ed|t)?|pick(?:ed)? up)(?: from me)?(?: so far)?"
                   r"|(?:show|tell) me what you(?:'ve| have)? learn(?:ed|t)|list (?:your|the) lessons)$")
_FORGET = re.compile(r"^(?:unlearn (?:that|it|the last one)|forget (?:that|the last) lesson"
                     r"|forget what you (?:just )?learn(?:ed|t)(?: about (?P<q>.+))?"
                     r"|unlearn (?P<q2>.+))$")


def learn_intent(t: str) -> tuple[str, dict] | None:
    if _LIST.match(t):
        return "lessons", {"action": "list"}
    m = _FORGET.match(t)
    if m:
        q = m.group("q") or m.group("q2")
        return "lessons", {"action": "forget", **({"about": q} if q else {})}
    return None


async def lessons(args: dict, ctx: ToolContext) -> str:
    from assistant.brain.lessons import get_lessons
    store = get_lessons()
    if args.get("action") == "forget":
        gone = store.forget(args.get("about"))
        if not gone:
            return "I haven't learned anything like that."
        return "Forgotten: " + "; ".join(x.describe() for x in gone) + "."
    items = store.items()
    from assistant.tools.teach import load_learned
    shown_to_me = [r.get("label") or n.replace("_", " ") for n, r in load_learned().items()]
    routines = f" Routines you showed me: {', '.join(shown_to_me)}." if shown_to_me else ""
    if not items:
        return ("Nothing from corrections yet: when I get something wrong, correct me and I'll remember."
                + routines)
    shown = "; ".join(x.describe() for x in items[-5:])
    more = f" And {len(items) - 5} more in the window." if len(items) > 5 else ""
    return f"I've learned {len(items)} thing{'s' if len(items) != 1 else ''}: {shown}.{more}{routines}"


def register(reg: ToolRegistry) -> None:
    reg.tool("lessons", "What Nova learned from the user's corrections and teaching ('when I say X, do Y'): "
             "list them, or forget one (about = words from it; empty = the last one).",
             {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "forget"]},
                                               "about": {"type": "string", "maxLength": 100}},
              "required": ["action"], "additionalProperties": False},
             risk=Risk.SAFE, category="memory")(lessons)
