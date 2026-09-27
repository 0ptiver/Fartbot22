"""Memory tools: remember, recall, forget (forgetting asks first, naming what will go)."""

from __future__ import annotations

from assistant.core.memory import MemoryStore, SecretRefused, get_store
from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry

_LAST = ("that", "it", "this", "the last thing", "what i just said", "last")
_ALL = ("everything", "all", "all of it", "all memories", "everything you know", "everything about me")


def _store(ctx: ToolContext) -> MemoryStore:
    store = ctx.services.get("memory") or get_store(ctx.settings)
    if store is None:
        raise ToolError("Memory is switched off in the settings.")
    return store


def remember(args: dict, ctx: ToolContext) -> str:
    try:
        mem, replaced = _store(ctx).add(args["text"])
    except SecretRefused as e:
        raise ToolError(str(e)) from e
    except ValueError as e:
        raise ToolError(str(e)) from e
    return f"{'Updated' if replaced else 'Remembered'}: {mem.text}."


def recall(args: dict, ctx: ToolContext) -> str:
    store = _store(ctx)
    about = (args.get("about") or "").strip()
    mems = store.search(about, 8) if about else store.all()[:15]
    if not mems:
        return f"I don't have anything remembered about {about}." if about else "I haven't been asked to remember anything yet."
    total = len(store.all())
    head = "What I remember" + (f" about {about}" if about else "") + ": "
    more = f" (and {total - len(mems)} more)" if not about and total > len(mems) else ""
    return head + "; ".join(m.text for m in mems) + more + "."


async def forget(args: dict, ctx: ToolContext) -> str:
    store = _store(ctx)
    what = (args.get("what") or "that").strip().lower().rstrip(".")
    everything = store.all()
    if not everything:
        return "There's nothing to forget."
    if what in _ALL:
        targets, label = everything, f"all {len(everything)} things I remember"
    elif what in _LAST:
        targets = everything[:1]
        label = f'"{targets[0].text}"'
    else:
        targets = store.search(what, 3)
        if not targets:
            raise ToolError(f"I don't remember anything about {what}.")
        targets = targets[:1]                         # the best match only: never a surprise purge
        label = f'"{targets[0].text}"'
    if ctx.confirm is not None and not await ctx.confirm("forget", {"memory": label}):
        raise ToolError("The user decided to keep it.")
    store.delete([m.id for m in targets])
    return f"Forgotten: {label}."


def register(reg: ToolRegistry) -> None:
    reg.tool("remember", "Remember a fact the user asks you to (their name, birthdays, preferences). "
             "Write it as a short sentence about the user, e.g. \"The user's sister's birthday is June 3\". "
             "Never passwords, PINs or card numbers.",
             {"type": "object", "properties": {"text": {"type": "string", "minLength": 2, "maxLength": 300}},
              "required": ["text"], "additionalProperties": False}, risk=Risk.SAFE, category="memory")(remember)
    reg.tool("recall", "List what you remember, optionally about a topic.",
             {"type": "object", "properties": {"about": {"type": "string", "maxLength": 100}},
              "additionalProperties": False}, risk=Risk.SAFE, category="memory")(recall)
    reg.tool("forget", "Forget a remembered fact (what = words from it, 'that' = the last one, or "
             "'everything'). Asks the user first.",
             {"type": "object", "properties": {"what": {"type": "string", "maxLength": 100}},
              "additionalProperties": False}, risk=Risk.SAFE, category="memory",
             describe=lambda a: f"forget {a.get('memory', 'that')}")(forget)
