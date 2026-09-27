"""Fast path for common commands: recognised directly, run straight away, and answered with
the tool's own result. No model call, so it's faster and the small local model can't
misunderstand them or talk itself out of doing them."""

from __future__ import annotations

import re

_LEAD = re.compile(r"^(?:(?:hey|ok|okay|so|um|uh|please|can you|could you|would you|will you|"
                   r"go ahead and|just|tell me|do you know)\s+)+")
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


def match_intent(text: str) -> tuple[str, dict] | None:
    t = _clean(text)
    if not t:
        return None
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
