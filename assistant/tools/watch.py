"""'Tell me when ...' tools, backed by core.watchers."""

from __future__ import annotations

import re

from assistant.core.watchers import KINDS, Watch, WatchError, Watchers
from assistant.tools.registry import Risk, ToolContext, ToolError, ToolRegistry


def _watchers(ctx: ToolContext) -> Watchers:
    w = ctx.services.get("watchers")
    if w is None:
        raise ToolError("Watching only works while I'm running in voice mode.")
    return w


def watch(args: dict, ctx: ToolContext) -> str:
    kind, target = args["kind"], (args.get("target") or "").strip()
    if kind == "page_changes":
        if not target or target.lower() in ("this page", "the page", "this site", "this website", "it"):
            from assistant.tools.uia import current_url
            target = current_url()
        elif not target.startswith("http"):
            target = "https://" + target.removeprefix("www.")
    if kind in ("app_closes", "app_done") and not target:
        raise ToolError("Which app?")
    try:
        w = _watchers(ctx).add(Watch(kind, target, args.get("value")))
    except WatchError as e:
        raise ToolError(str(e)) from e
    return f"I'll tell you when I notice {w.describe()}."


def list_watches(args: dict, ctx: ToolContext) -> str:
    items = list(_watchers(ctx).items.values())
    if not items:
        return "I'm not watching anything at the moment."
    return "I'm watching for: " + "; ".join(w.describe() for w in items) + "."


def cancel_watch(args: dict, ctx: ToolContext) -> str:
    hits = _watchers(ctx).cancel(args.get("which", ""))
    if not hits:
        return "I wasn't watching for that."
    return "Stopped watching for " + "; ".join(w.describe() for w in hits) + "."


def register(reg: ToolRegistry) -> None:
    reg.tool("watch", "Keep an eye on something and tell the user when it happens. kind: download "
             "(browser download finishes), steam_download, app_closes / app_done (target = app name), "
             "gpu_below / gpu_above / cpu_below (value = °C or %), battery_above / battery_below "
             "(value = %), internet_back, page_changes (target = URL or 'this page').",
             {"type": "object", "properties": {
                 "kind": {"type": "string", "enum": KINDS},
                 "target": {"type": "string", "maxLength": 300},
                 "value": {"type": "number", "minimum": 0, "maximum": 120}},
              "required": ["kind"], "additionalProperties": False}, risk=Risk.SAFE, category="watch")(watch)
    reg.tool("list_watches", "What you're currently watching for.", risk=Risk.SAFE, category="watch")(list_watches)
    reg.tool("cancel_watch", "Stop watching: which = words from it, or 'all'. Empty = the latest.",
             {"type": "object", "properties": {"which": {"type": "string", "maxLength": 60}},
              "additionalProperties": False}, risk=Risk.SAFE, category="watch")(cancel_watch)


# --- phrases ---------------------------------------------------------------------------------------
_LEAD = (r"(?:(?:hey |ok |okay |please |can you |could you |just )*)"
         r"(?:tell me|let me know|notify me|alert me|ping me|warn me|shout|say something)(?: when| once| if)\s+")
_DONE = r"(?:finishes|is done|is finished|completes|is complete|finished|done)"


def watch_intent(raw: str) -> tuple[str, dict] | None:
    """Needs the original sentence: 'tell me when ...' (a bare 'when ...' is a question)."""
    from assistant.voice.textnorm import normalize_words

    t = re.sub(r"[.!?]+$", "", raw.strip().lower())
    n = " ".join(normalize_words(t))
    n = re.sub(r"\s+(?:please|sir|nova|thanks)$", "", n)
    if re.fullmatch(r"what (?:are|re) you (?:watching|keeping an eye on)(?: for)?|(?:list|show) (?:my )?watch(?:es|ers)", n):
        return "list_watches", {}
    m = re.fullmatch(r"(?:stop|cancel|quit) watching(?: for)?(?: (.+))?", n)
    if m:
        return "cancel_watch", {"which": m.group(1) or ""}
    site = re.match(r"^" + _LEAD + r"((?:https?://)?(?:[\w-]+\.)+[a-z]{2,}(?:/\S*)?) (?:changes|updates|is updated)$", t)
    if site:
        return "watch", {"kind": "page_changes", "target": site.group(1)}
    m = re.match(r"^" + _LEAD + r"(.+)$", n)
    if not m:
        return None
    c = re.sub(r"^(?:the|my)\s+", "", m.group(1).strip())
    num = re.search(r"(\d{1,3})", c)
    val = float(num.group(1)) if num else None
    if re.fullmatch(r"(?:(?:steam|game|games)(?: \w+)? )?(?:download|downloads)(?: \w+)?(?: is)? " + _DONE
                    + r"|(?:it|that|this) (?:finishes )?download(?:s|ing)?(?: " + _DONE + ")?|download " + _DONE, c) \
            and not re.search(r"\b(?:steam|game)\b", c):
        return "watch", {"kind": "download"}
    if re.search(r"\b(?:steam|game|games|update)\b.*\b(?:download|install|updat)", c) or \
            re.fullmatch(r"steam (?:is )?" + _DONE, c):
        return "watch", {"kind": "steam_download"}
    if re.search(r"\bgpu\b|graphics card", c):
        if re.search(r"\b(?:cool|cools|cooled|cold|below|under|down)\b", c):
            return "watch", {"kind": "gpu_below", **({"value": val} if val else {})}
        if re.search(r"\b(?:hot|above|over|hits|reaches|gets to)\b", c):
            return "watch", {"kind": "gpu_above", **({"value": val} if val else {})}
    if re.search(r"\bcpu\b", c) and re.search(r"\b(?:calm|quiet|below|under|down|idle)\b", c):
        return "watch", {"kind": "cpu_below", **({"value": val} if val else {})}
    if re.search(r"\bbattery\b", c):
        if re.search(r"\b(?:low|below|under|drops|dying)\b", c):
            return "watch", {"kind": "battery_below", **({"value": val} if val else {})}
        if re.search(r"\b(?:full|charged|above|reaches|hits|at)\b", c):
            return "watch", {"kind": "battery_above", "value": val or 100.0}
    if re.search(r"\b(?:internet|wifi|wi fi|connection)\b.*\b(?:back|works|working|up)\b|\bback online\b", c):
        return "watch", {"kind": "internet_back"}
    m = re.fullmatch(r"(this page|this site|this website|the page|[\w.-]+\.[a-z]{2,}(?:/\S*)?) (?:changes|updates|is updated|changed)", c)
    if m:
        return "watch", {"kind": "page_changes", "target": m.group(1)}
    m = re.fullmatch(r"(.+?) (?:closes|quits|exits|is closed|crashes|stops running)", c)
    if m:
        return "watch", {"kind": "app_closes", "target": m.group(1)}
    m = re.fullmatch(r"(.+?) (?:is )?" + _DONE + r"(?: (?:rendering|exporting|compiling|processing|working|loading))?", c)
    if m and len(m.group(1).split()) <= 3:
        return "watch", {"kind": "app_done", "target": m.group(1)}
    return None
