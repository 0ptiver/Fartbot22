"""Fast path for common commands: recognised directly, run straight away, and answered with
the tool's own result. No model call, so it's faster and the small local model can't
misunderstand them or talk itself out of doing them."""

from __future__ import annotations

import re

_LEAD = re.compile(r"^(?:(?:hey|ok|okay|so|um|uh|please|can you|could you|would you|will you|"
                   r"go ahead and|just|tell me|do you know|now|actually|also|then|and|"
                   r"i want you to|i need you to|i'd like you to|i would like you to)\s+)+")
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
    # Whatever is actually playing (Spotify, a video, any player): see tools/video.media.
    (re.compile(r"^(?:pause|stop)" + _THING + "$"), "media", {"action": "pause"}),
    (re.compile(r"^(?:resume|unpause|continue|keep playing)" + _THING + "$"), "media", {"action": "play"}),
    (re.compile(r"^(?:play )?(?:the )?(?:next|skip)" + _THING + "$"), "media", {"action": "next"}),
    (re.compile(r"^skip(?: (?:this|it|the song|this song))?$"), "media", {"action": "next"}),
    (re.compile(r"^(?:play )?(?:the )?(?:previous|last)(?: (?:song|track))$|^go back(?: a song| to the last song)?$"),
     "media", {"action": "previous"}),
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
# "screen 2", "the second monitor", "the other display", "left screen"
_SCREEN = (r"(?:(?:screen|monitor|display) (\d)"
           r"|(main|primary|first|second|2nd|third|3rd|other|next|left|right|laptop) (?:screen|monitor|display))")
_SCREEN_WORDS = {"primary": "main", "first": "main", "laptop": "main", "second": "2", "2nd": "2",
                 "third": "3", "3rd": "3", "other": "next"}


def _screen_arg(m: re.Match) -> dict:
    num, word = m.group(1), m.group(2)
    if num:
        return {"screen": num}
    if word:
        return {"screen": _SCREEN_WORDS.get(word, word)}
    return {}
_CLICKS = {"click": "click", "click on": "click", "press": "click", "tap": "click", "select": "click",
           "double click": "double_click", "double click on": "double_click",
           "right click": "right_click", "right click on": "right_click",
           "middle click": "middle_click", "middle click on": "middle_click"}
_AMOUNT = {"a bit": 2, "a little": 2, "a little bit": 2, "a lot": 10, "lots": 10, "more": 5}


def grid_intent(t: str, visible: bool = False, labels: bool = False) -> tuple[str, dict] | None:
    """Voice mouse: 'show the grid', 'click 14', 'zoom 14', 'scroll down', 'drag 5 to 12',
    'show numbers', 'click subscribe'. With the grid showing, a bare number zooms (with numbers
    showing, it clicks) and 'back'/'cancel' work too."""
    from assistant.voice.textnorm import normalize_words

    visible = visible or labels
    n = " ".join(normalize_words(t))
    if re.fullmatch(r"(?:show|give me|put up|turn on)(?: me)?(?: the)? (?:numbers|labels|number labels)"
                    r"|(?:number|label) (?:the |all the |everything|things)?(?:buttons|links|things)?"
                    r"|what can i click(?: on)?|show (?:me )?(?:the |all the )?(?:buttons|links|clickable things)", n):
        return "show_numbers", {}
    if re.fullmatch(r"(?:hide|close|remove|turn off|clear)(?: the)? (?:numbers|labels)|numbers off", n):
        return "mouse_grid", {"action": "hide"}
    if labels and (m := re.fullmatch(_N, n)):
        return "mouse", {"action": "click", "cell": int(m.group(1))}
    m = re.match(r"^(?:(?:show|open|bring up|turn on|give me|put)(?: me)?(?: the| a| my)? (?:mouse )?grid"
                 r"|(?:mouse )?grid)(?: on)?(?: (?:on|for) (?:the |my )?" + _SCREEN + ")?$", n)
    if m:
        return "mouse_grid", {"action": "show", **_screen_arg(m)}
    m = re.match(r"^(?:(?:switch|move|go|change)(?: the grid)? to (?:the |my )?" + _SCREEN
                 + r"|(?:next|other|switch|change) (?:screen|monitor|display)s?)$", n)
    if m and visible:
        return "mouse_grid", {"action": "show", "screen": _screen_arg(m).get("screen", "next")}
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
    if m and m.group(1) in ("press", "tap") and not visible:
        m = None                        # "press 3" without the grid is the 3 key
    if m and (m.group(2) or visible or m.group(1) in ("click", "double click", "right click")):
        args = {"action": _CLICKS[m.group(1)]}
        if m.group(2):
            args["cell"] = int(m.group(2))
        return "mouse", args
    # "click subscribe", "double click the file", "press the sign in button": by name
    m = re.fullmatch(r"(click|click on|double click|double click on|right click|right click on)"
                     r" (?!(?:it|that|there|here|number)\b)(.*[a-z].*)", n)
    if not m:
        m = re.fullmatch(r"(press|hit|tap|push|open|select|choose) (the .+ (?:button|link|tab|icon|option|checkbox|box))", n)
    if m and not re.fullmatch(r"(?:the )?(?:grid|mouse grid|numbers)", m.group(2)):
        action = {"double": "double_click", "right": "right_click"}.get(m.group(1).split()[0], "click")
        args = {"name": m.group(2)}
        if action != "click":
            args["action"] = action
        return "click_element", args
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


# Videos: "full screen the video and press play", "pause the video", "skip ahead".
_VIDEO_NOUN = r"(?:video|youtube(?: video)?|movie|film|clip|show|episode|stream|netflix|twitch)"
_VIDEO_WORDS = re.compile(r"\b" + _VIDEO_NOUN + r"\b|\bfull ?screen\b")
_VIDEO_FILLER = re.compile(
    r"\b(?:(?:on|in) (?:my |the )?(?:screen|browser|firefox|chrome|edge|youtube)|the|this|that|my|it|"
    r"please|for me|now|again|on|in|of|" + _VIDEO_NOUN + r")\b")
_VIDEO_ACTIONS = [
    (r"(?:exit|leave|get out of|close|undo|stop|no|turn off|minimi[sz]e)(?: the)? full ?screen|un ?full ?screen|"
     r"make (?:it )?small(?:er)?", "exit_fullscreen"),
    (r"(?:make (?:it )?|go |put (?:it )?(?:in |on )?|set (?:it )?(?:to )?)?full ?screen|make (?:it )?bigger|"
     r"maximi[sz]e", "fullscreen"),
    (r"(?:hit |press |click |start |)(?:play(?:ing)?|resume|unpause|continue|keep playing)", "play"),
    (r"(?:hit |press |click )?(?:pause|stop|hold)", "pause"),
    (r"(?:un)?mute", "mute"),
    (r"skip(?: ahead| forward)?|(?:go )?forward|fast forward", "forward"),
    (r"(?:go )?back|rewind|skip back", "back"),
    (r"(?:play )?(?:the )?next(?: one)?|skip to (?:the )?next", "next"),
    (r"(?:play )?(?:the )?previous(?: one)?|(?:the )?last one", "previous"),
]


def video_intent(t: str) -> tuple[str, dict] | None:
    """Only when a video (or fullscreen) is mentioned, or it's a bare 'hit play'/'press pause':
    'pause' and 'next song' on their own belong to Spotify."""
    if re.fullmatch(r"(?:hit|press) (?:play|pause)", t):
        return "video", {"actions": ["play" if t.endswith("play") else "pause"]}
    if not _VIDEO_WORDS.search(t):
        return None
    parts = [p for p in re.split(r"\s*(?:,|\band then\b|\bthen\b|\band\b|\balso\b)\s*", t) if p.strip()]
    actions = []
    for part in parts:
        p = " ".join(_VIDEO_FILLER.sub(" ", part).split())
        if not p and _VIDEO_WORDS.search(part):   # "the video" alone after a verb split off
            continue
        for pattern, action in _VIDEO_ACTIONS:
            if re.fullmatch(pattern, p):
                actions.append(action)
                break
        else:
            return None                             # something we don't understand: the model decides
    return ("video", {"actions": actions}) if actions else None


# Named shortcuts: phrase -> keys. Checked after video phrases ("go back in the video").
_SHORTCUTS = [
    (r"copy(?: (?:that|it|this))?", "ctrl+c"), (r"paste(?: (?:that|it|this))?", "ctrl+v"),
    (r"cut(?: (?:that|it|this))?", "ctrl+x"), (r"undo(?: (?:that|it|this))?", "ctrl+z"),
    (r"redo(?: (?:that|it|this))?", "ctrl+y"), (r"select (?:all|everything)", "ctrl+a"),
    (r"save(?: (?:it|this|that|the file|the document))?", "ctrl+s"),
    (r"(?:open )?(?:a )?new tab", "ctrl+t"), (r"close (?:the |this |that )?tab", "ctrl+w"),
    (r"(?:reopen|bring back|restore) (?:the |that |my )?(?:last |closed )*tab", "ctrl+shift+t"),
    (r"(?:go to (?:the )?)?next tab|tab right", "ctrl+tab"),
    (r"(?:go to (?:the )?)?(?:previous|last) tab|tab left", "ctrl+shift+tab"),
    (r"(?:refresh|reload)(?: (?:the|this) page| it)?", "f5"),
    (r"go back(?: a page)?|(?:previous|last) page|back a page", "alt+left"),
    (r"go forward(?: a page)?|next page|forward a page", "alt+right"),
    (r"(?:switch|change) (?:windows|apps|app|window)|alt tab", "alt+tab"),
    (r"(?:find|search) (?:on|in) (?:this |the )?page", "ctrl+f"),
    (r"(?:go to |click )?(?:the )?address bar", "ctrl+l"),
    (r"zoom in(?: (?:the|this) page)?", "ctrl+plus"), (r"zoom out(?: (?:the|this) page)?", "ctrl+minus"),
    (r"(?:reset zoom|actual size)", "ctrl+0"),
    (r"(?:scroll to (?:the )?top|go to (?:the )?top)(?: of (?:the|this) page)?", "ctrl+home"),
    (r"(?:scroll to (?:the )?bottom|go to (?:the )?bottom)(?: of (?:the|this) page)?", "ctrl+end"),
    (r"page down", "pagedown"), (r"page up", "pageup"),
    (r"snap (?:it |this |the window |this window )?(?:to the )?left", "win+left"),
    (r"snap (?:it |this |the window |this window )?(?:to the )?right", "win+right"),
    (r"move (?:it|this|this window|the window) to the (?:other|next) (?:screen|monitor|display)", "win+shift+right"),
    (r"open (?:the )?start(?: menu)?|start menu", "win"),
    (r"(?:open )?task manager", "ctrl+shift+escape"),
    (r"(?:take a )?screenshot|snip(?: the screen)?", "win+shift+s"),
    (r"(?:open )?(?:the )?emoji(?: picker| keyboard)?", "win+period"),
]
_THIS_WINDOW = r"(?:this|it|that|the window|this window|the app|this app|current window)"
_KEY_WORDS = r"(?:[a-z0-9]+(?:[ +][a-z0-9]+){0,3}?)"


def keyboard_intent(raw: str, t: str) -> tuple[str, dict] | None:
    """'type hello there', 'press control c', 'press tab 3 times', 'copy', 'new tab'."""
    from assistant.tools.keyboard import parse_keys
    from assistant.tools.registry import ToolError

    if re.fullmatch(r"(?:start |begin |turn on |switch on )?(?:dictation|dictating)(?: mode)?(?: on)?|"
                    r"(?:start|let me|i want to) (?:dictate|dictating|type by voice)|take (?:a )?dictation|dictate", t):
        return "dictation", {"on": True}
    if re.fullmatch(r"(?:stop|end|finish|turn off|exit) (?:the )?(?:dictation|dictating)(?: mode)?|dictation off", t):
        return "dictation", {"on": False}
    m = re.match(r"^\s*(?:please\s+|can you\s+|could you\s+)?(?:type|write|type out|enter the text)"
                 r"\s*[:,]?\s+(.+?)\s*$", raw, re.I | re.S)
    if m:
        text = m.group(1)
        if re.fullmatch(r"[^.!?]+\.", text):                  # Whisper's full stop on a phrase
            text = text[:-1]
        return "type_text", {"text": text}
    for pattern, keys in _SHORTCUTS:
        if re.fullmatch(pattern, t):
            return "press_keys", {"keys": keys}
    m = re.fullmatch(r"(?:minimi[sz]e|hide) " + _THIS_WINDOW, t)
    if m:
        return "window", {"action": "minimize", "app": "this"}
    if re.fullmatch(r"(?:maximi[sz]e|make (?:it|this) (?:full|bigger)) ?" + _THIS_WINDOW + "?", t) and "full screen" not in t:
        return "window", {"action": "maximize", "app": "this"}
    if re.fullmatch(r"close " + _THIS_WINDOW, t) and t != "close it":
        return "window", {"action": "close", "app": "this"}
    m = re.fullmatch(r"(?:press|hit|push|tap) (?:the )?(" + _KEY_WORDS + r")(?: key| button)?"
                     r"(?: (\d{1,2}|two|three|four|five|ten) times)?", t)
    if m:
        keys = m.group(1).replace(" and ", " ")
        try:
            parse_keys(keys)
        except ToolError:
            return None
        times = m.group(2)
        args = {"keys": keys}
        if times:
            args["times"] = int(times) if times.isdigit() else {"two": 2, "three": 3, "four": 4,
                                                                 "five": 5, "ten": 10}[times]
        return "press_keys", args
    return None


def memory_intent(raw: str, t: str) -> tuple[str, dict] | None:
    """'remember that my sister's birthday is June 3', 'what do you remember', 'forget that'."""
    m = re.match(r"^\s*(?:please\s+|can you\s+|could you\s+)?(?:remember|don'?t forget|note)\s*[,:]?\s+"
                 r"(?:that\s+)?(?!to\b)(.{3,300}?)[.!]?\s*$", raw, re.I | re.S)
    if m and not re.match(r"^(?:what|when|where|who|how|if|whether)\b", m.group(1), re.I):
        return "remember", {"text": m.group(1)}
    if re.fullmatch(r"what (?:do|did) you (?:remember|know)(?: about me)?|what have you remembered|"
                    r"(?:list|show me) (?:your |my )?memor(?:y|ies)", t):
        return "recall", {}
    m = re.fullmatch(r"what (?:do|did) you (?:remember|know) about (.+)", t)
    if m:
        return "recall", {"about": m.group(1)}
    m = re.fullmatch(r"forget (?:about )?(that|it|this|what i just said|everything(?: about me)?|(?:all|all of) "
                     r"(?:it|that|my memories)|.+)", t)
    if m and not re.search(r"\b(?:routine|lesson|task|my voice|my voiceprint)$", t):   # lessons / voice lock, not memories
        return "forget", {"what": m.group(1)}
    return None


_SITES = {"youtube", "google", "netflix", "twitch", "reddit", "gmail", "amazon", "twitter", "facebook",
          "instagram", "tiktok", "wikipedia", "github", "chatgpt", "claude", "google maps", "maps",
          "outlook", "prime video", "disney plus", "hulu", "ebay"}
_NOT_AN_APP = re.compile(r"\b(?:file|folder|document|documents|downloads|pictures|photo|tab|window|link|"
                         r"grid|numbers|settings for|and|then|with|it|that|this|routine|timer|video|movie|music|song|playlist|"
                         r"dictation|recording|lesson|mode|watching|over|again)\b")
_THIS = r"(?:this|that|it|the|this window|that window|the window|current window|the app|this app)"


def everyday_intent(t: str) -> tuple[str, dict] | None:
    """The commands people say most, done without the model (faster, and no chatter):
    'what time is it', 'volume up', 'set the volume to 30', 'open discord', 'close spotify',
    'switch to firefox', 'minimize this'."""
    from assistant.voice.textnorm import normalize_words

    n = " ".join(normalize_words(t))
    if re.fullmatch(r"(?:what(?:'?s| is) the time(?: now)?|what time is it(?: now)?|tell me the time)", t):
        return "get_time", {}
    if m := re.fullmatch(r"(?:set |turn |put )?(?:the )?volume (?:to |at )?(\d{1,3})(?: percent| %)?", n):
        return "volume", {"action": "set", "level": min(100, int(m.group(1)))}
    if re.fullmatch(r"(?:turn (?:the )?(?:volume|sound|it) up|volume up|louder|turn up the volume|raise the volume)"
                    r"(?: a (?:bit|little))?", n):
        return "volume", {"action": "up"}
    if re.fullmatch(r"(?:turn (?:the )?(?:volume|sound|it) down|volume down|quieter|turn down the volume|"
                    r"lower the volume)(?: a (?:bit|little))?", n):
        return "volume", {"action": "down"}
    if re.fullmatch(r"(?:mute|mute (?:the )?(?:pc|computer|sound|volume|audio))", n):
        return "volume", {"action": "mute"}
    if re.fullmatch(r"(?:unmute|unmute (?:the )?(?:pc|computer|sound|volume|audio))", n):
        return "volume", {"action": "unmute"}
    if m := re.fullmatch(r"(minimi[sz]e|maximi[sz]e|restore) " + _THIS, n):
        action = {"minimi": "minimize", "maximi": "maximize"}.get(m.group(1)[:6], "restore")
        return "window", {"action": action, "app": "this"}
    if re.fullmatch(r"(?:close|quit|exit) " + _THIS, n):
        return "window", {"action": "close", "app": "this"}
    m = re.fullmatch(r"(open|launch|start|close|quit|exit|switch to|bring up|go to) (?:up )?(?:the |my )?(.+?)"
                     r"(?: app| application| program)?", n)
    if m and len(m.group(2).split()) <= 3 and not _NOT_AN_APP.search(m.group(2)):
        verb, what = m.group(1), m.group(2)
        if verb in ("open", "launch", "go to") and what in _SITES:
            return "open_website", {"site": what}
        if verb in ("open", "launch", "start"):
            return "open_app", {"name": what}
        if verb in ("close", "quit", "exit"):
            return "window", {"action": "close", "app": what}
        if verb in ("switch to", "bring up"):
            return "window", {"action": "focus", "app": what}
    return None


def match_intent(text: str, grid_visible: bool = False, labels: bool = False) -> tuple[str, dict] | None:
    t = _clean(text)
    if not t:
        return None
    mem = memory_intent(text, t)
    if mem:
        return mem
    from assistant.tools.watch import watch_intent
    w = watch_intent(text)
    if w:
        return w
    from assistant.tools.teach import teach_intent
    lesson = teach_intent(t)
    if lesson:
        return lesson
    from assistant.tools.voice import subtitles_intent, voice_intent, voicelock_intent
    v = voicelock_intent(t) or voice_intent(t) or subtitles_intent(t)
    if v:
        return v
    timer = _timer_intent(t)
    if timer:
        return timer
    grid = grid_intent(t, grid_visible, labels)
    if grid:
        return grid
    video = video_intent(t)
    if video:
        return video
    keys = keyboard_intent(text, t)
    if keys:
        return keys
    for pattern, tool, args in _PC_RULES:
        if pattern.match(t):
            return tool, dict(args)
    for pattern, tool, args in _RULES:
        if pattern.match(t):
            return tool, dict(args)
    daily = everyday_intent(t)
    if daily:
        return daily
    if len(t.split()) <= 14 and _NOW_PLAYING_ANYWHERE.search(t):
        return "now_playing", {}
    if t in ("play", "play it", "play it again", "play again", "press play", "hit play", "play that", "play this"):
        return "media", {"action": "play"}                  # resume what's paused (not a song called "it")
    m = re.match(r"^(?:(?:open|go (?:on|to)|launch|start) spotify and |on spotify )?"
                 r"(?:play|put on|throw on|stick on) (.+?)(?: on spotify)?$", t)
    if m:
        q = m.group(1).strip()
        if q in ("music", "some music", "spotify", "my music", "something", "some tunes", "a song"):
            return "media", {"action": "play"}
        q = re.sub(r"^(?:something|anything|a song|some songs|some music|songs|music) by ", "", q)
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
