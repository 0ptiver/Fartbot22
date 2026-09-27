"""Fast path for common commands: recognised directly, run straight away, and answered with
the tool's own result. No model call, so it's faster and the small local model can't
misunderstand them or talk itself out of doing them."""

from __future__ import annotations

import re

_LEAD = re.compile(r"^(?:(?:hey|ok|okay|so|um|uh|please|can you|could you|would you|will you|"
                   r"go ahead and|just|tell me|do you know|now|actually|also|then|and)\s+)+")
# "What's playing?" asked inside a longer sentence ("I'm playing a song, tell me what's playing").
_NOW_PLAYING_ANYWHERE = re.compile(
    r"\bwhat(?:'?s| is)(?: currently)? playing\b|\bwhat song is (?:this|playing)\b"
    r"|\bwhat(?: song)? (?:am i|i'?m|am i'?m) (?:playing|listening to)\b|\bwhat(?:'?s| is) this song\b"
    r"|\bwhat(?:'?s| is) (?:this|the) song called\b|\bwhat song am i\b|\bname (?:of )?this song\b")
_TAIL = re.compile(r"(?:\s+(?:please|for me|now|sir|thanks|thank you|nova))+$")
_THING = r"(?:\s+(?:the|my|this))?(?:\s+(?:music|song|track|spotify|playback|it|one|tune))?"

_RULES: list[tuple[re.Pattern, str, dict]] = [
    (re.compile(r"^(?:what(?:'?s| is)(?: currently)? playing|what(?:'?s| is) this song|what song is (?:this|playing|on)"
                r"|what am i listening to|(?:tell me )?what(?:'?s| is) (?:the )?(?:song|track)(?: playing)?"
                r"|what(?:'?s| is) (?:on|playing on) spotify|(?:tell me )?what song this is)$"), "now_playing", {}),
    (re.compile(r"^(?:pause|stop)" + _THING + "$"), "music_control", {"action": "pause"}),
    (re.compile(r"^(?:resume|unpause|continue|keep playing)" + _THING + "$"), "music_control", {"action": "resume"}),
    (re.compile(r"^(?:play )?(?:the )?(?:next|skip)" + _THING + "$"), "music_control", {"action": "next"}),
    (re.compile(r"^skip(?: (?:this|it|the song|this song))?$"), "music_control", {"action": "next"}),
    (re.compile(r"^(?:play )?(?:the )?(?:previous|last)(?: (?:song|track))$|^go back(?: a song| to the last song)?$"),
     "music_control", {"action": "previous"}),
    (re.compile(r"^shuffle(?: (?:on|my music))?$|^turn (?:on )?shuffle(?: on)?$"), "music_control", {"action": "shuffle_on"}),
    (re.compile(r"^(?:turn )?shuffle off$|^turn off shuffle$"), "music_control", {"action": "shuffle_off"}),
]


def _clean(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r"[.!?,]+$", "", t).strip()
    t = _LEAD.sub("", t)
    t = _TAIL.sub("", t)
    return t.replace("’", "'").strip()


_UNIT = {"second": "seconds", "sec": "seconds", "minute": "minutes", "min": "minutes", "hour": "hours"}


def _timer_intent(t: str) -> tuple[str, dict] | None:
    """'set a timer for 5 minutes', 'ten minute timer for the pasta', 'cancel the timer'."""
    from assistant.voice.textnorm import normalize_words

    n = " ".join(normalize_words(t))
    m = (re.match(r"^(?:set |start |put on )?(?:a |an |me a )?timer (?:for|of) (\d+(?:\.\d+)?) "
                  r"(second|sec|minute|min|hour)s?(?: (?:for|called) (?:the )?(.+))?$", n)
         or re.match(r"^(?:set |start )?(?:a |an )?(\d+(?:\.\d+)?) (second|sec|minute|min|hour)s? timer"
                     r"(?: (?:for|called) (?:the )?(.+))?$", n))
    if m:
        args = {_UNIT[m.group(2)]: float(m.group(1))}
        if m.group(3):
            args["label"] = m.group(3)
        return "set_timer", args
    if re.match(r"^(?:cancel|stop|kill|delete|clear|turn off) (?:the |my |that )?(?:timer|alarm)s?$", n):
        return "cancel_timer", {"which": "timer" if "timer" in n else "alarm"}
    if re.match(r"^(?:cancel|clear|delete) (?:all|all of) (?:my |the )?(?:timers|reminders|alarms)"
                r"(?: and (?:reminders|alarms|timers))*$", n):
        return "cancel_timer", {"which": "all"}
    if re.search(r"\bhow (?:much time|long) (?:is )?left\b|\bwhat timers\b|\bany timers\b"
                 r"|\b(?:list|show) (?:my )?(?:timers|reminders|alarms)\b", n):
        return "list_timers", {}
    return None


_PC_RULES = [
    (re.compile(r"^(?:please )?lock (?:my |the )?(?:pc|computer|laptop|screen)(?: please)?$"), "lock_pc", {}),
    (re.compile(r"^(?:cancel|stop|abort) (?:the )?(?:shutdown|shut down|restart|reboot)$"), "cancel_shutdown", {}),
    (re.compile(r"^(?:show|go to) (?:me )?(?:the |my )?desktop$|^minimi[sz]e everything$"),
     "window", {"action": "show_desktop"}),
]


_N = r"(?:number )?(\d{1,3})"
_CLICKS = {"click": "click", "click on": "click", "press": "click", "tap": "click", "select": "click",
           "double click": "double_click", "double click on": "double_click",
           "right click": "right_click", "right click on": "right_click",
           "middle click": "middle_click", "middle click on": "middle_click"}
_AMOUNT = {"a bit": 2, "a little": 2, "a little bit": 2, "a lot": 10, "lots": 10, "more": 5}


def grid_intent(t: str, visible: bool = False) -> tuple[str, dict] | None:
    """Voice mouse: 'show the grid', 'click 14', 'zoom 14', 'scroll down', 'drag 5 to 12'.
    With the grid showing, a bare number zooms and 'back'/'cancel' work too."""
    from assistant.voice.textnorm import normalize_words

    n = " ".join(normalize_words(t))
    if re.match(r"^(?:show|open|bring up|turn on|give me)(?: me)?(?: the| a| my)? (?:mouse )?grid$"
                r"|^(?:mouse )?grid(?: on)?$", n):
        return "mouse_grid", {"action": "show"}
    if re.match(r"^(?:hide|close|cancel|remove|turn off|get rid of|clear)(?: the| that)? (?:mouse )?grid$"
                r"|^grid off$", n) or (visible and re.match(r"^(?:cancel|never mind|close it|hide it)$", n)):
        return "mouse_grid", {"action": "hide"}
    if visible and re.match(r"^(?:back|go back|zoom out|undo)$", n):
        return "mouse_grid", {"action": "back"}
    m = re.match(r"^(?:zoom|zoom in|zoom into|zoom in on|zoom on|go into)(?: to)? " + _N + "$", n)
    if m or (visible and (m := re.match(r"^" + _N + "$", n))):
        return "mouse_grid", {"action": "zoom", "cell": int(m.group(1))}
    m = re.match(r"^(" + "|".join(sorted(_CLICKS, key=len, reverse=True)) + r")(?: " + _N + r")?"
                 r"(?: (?:it|that|there|here))?$", n)
    if m and (m.group(2) or visible or m.group(1) in ("click", "double click", "right click")):
        args = {"action": _CLICKS[m.group(1)]}
        if m.group(2):
            args["cell"] = int(m.group(2))
        return "mouse", args
    m = re.match(r"^(?:move|go|put|hover)(?: the)?(?: mouse| cursor)?(?: to| over| on)? " + _N + "$", n)
    if m:
        return "mouse", {"action": "move", "cell": int(m.group(1))}
    m = re.match(r"^scroll (up|down)(?: (\d{1,2}|" + "|".join(_AMOUNT) + r"))?(?: (?:on|at|in) " + _N + ")?$", n)
    if m:
        amount = m.group(2)
        args = {"action": f"scroll_{m.group(1)}",
                "amount": int(amount) if amount and amount.isdigit() else _AMOUNT.get(amount or "", 3)}
        if m.group(3):
            args["cell"] = int(m.group(3))
        return "mouse", args
    m = re.match(r"^drag(?: from)? " + _N + " (?:to|onto|over to) " + _N + "$", n)
    if m:
        return "mouse", {"action": "drag", "cell": int(m.group(1)), "to": int(m.group(2))}
    return None


# "Hit play on the video on my screen": the media key reaches YouTube/Netflix in any browser.
_VIDEO = re.compile(
    r"^(?:hit |press |click )?(?:play|pause|unpause|resume|stop)(?: on)? (?:the |this |my |that )?"
    r"(?:video|youtube|youtube video|movie|film|clip|show|episode|stream)"
    r"(?: (?:on|in) (?:my |the )?(?:screen|browser|firefox|chrome|edge|youtube))?$"
    r"|^(?:hit|press) (?:play|pause)$")


def match_intent(text: str, grid_visible: bool = False) -> tuple[str, dict] | None:
    t = _clean(text)
    if not t:
        return None
    timer = _timer_intent(t)
    if timer:
        return timer
    grid = grid_intent(t, grid_visible)
    if grid:
        return grid
    if _VIDEO.match(t):
        return "media_key", {"action": "play_pause"}
    for pattern, tool, args in _PC_RULES:
        if pattern.match(t):
            return tool, dict(args)
    for pattern, tool, args in _RULES:
        if pattern.match(t):
            return tool, dict(args)
    if len(t.split()) <= 14 and _NOW_PLAYING_ANYWHERE.search(t):
        return "now_playing", {}
    m = re.match(r"^play (.+?)(?: on spotify)?$", t)
    if m:
        q = m.group(1).strip()
        if q in ("music", "some music", "spotify", "my music", "something"):
            return "music_control", {"action": "resume"}
        if re.fullmatch(r"(?:some of )?(?:my )?(?:liked songs|likes|favou?rites|favou?rite songs)", q):
            return "play_music", {"kind": "liked_songs"}
        if re.search(r"(?:last|recent|previous) (?:song|track|thing) i (?:listened to|played|heard)"
                     r"|what i was (?:listening to|playing)", q):
            return "play_music", {"kind": "recently_played"}
        q = re.sub(r"^(?:some|the song|the track|the album|the playlist|a song called|me)\s+", "", q)
        kind = "auto"
        if re.match(r"^my .+ playlist$", q) or q.endswith(" playlist"):
            kind, q = "playlist", re.sub(r"^my |\s*playlist$", "", q).strip()
        if len(q) >= 2:
            return "play_music", {"query": q, "kind": kind}
    return None
