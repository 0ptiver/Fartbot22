"""Nova learns from you.

- Corrections: "open the news" -> (wrong site) -> "no, I meant bbc.co.uk". Nova does it right,
  and remembers that "open the news" means what fixed it (the exact tool calls). Next time it
  just does that. "That's wrong" on its own: Nova asks what it should have done.
- Teaching: "when I say the news, open bbc.co.uk" -> a phrase that means another request.
- Preferences said in passing ("I prefer dark mode", "my favourite team is Arsenal",
  "from now on always use Firefox") go into memory without having to say "remember".

Lessons live in data/lessons.json: phrases and tool calls only. Everything can be listed and
forgotten by voice or in the window.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field

from assistant.core.config import ROOT

# v2: lessons from the first version could be learned from "Sorry, can you ..." (a new request,
# not a correction) and made Nova do the wrong thing, so they're left behind.
LESSONS_FILE = ROOT / "data" / "lessons-v2.json"
MAX_LESSONS = 200
FIX_WINDOW_S = 300            # a correction must come within 5 minutes of what it corrects
# Tools that look things up rather than do things: nothing to learn from them.
NOT_ACTIONS = {"get_time", "recall", "now_playing", "list_timers", "list_watches", "system_status",
               "web_search", "look_at_screen", "escalate", "remember", "forget", "lessons"}

_FILLER = {"please", "nova", "now", "for", "me", "can", "you", "could", "would", "will", "just", "hey",
           "ok", "okay", "the", "a", "an", "my", "sir", "thanks", "thank", "go", "ahead", "and"}


def key(text: str) -> str:
    """What a request 'is', ignoring politeness: 'Nova, could you open the news please' ->
    'open news'."""
    words = re.sub(r"[^\w\s.'/-]", " ", text.lower()).split()
    return " ".join(w for w in words if w not in _FILLER).strip(" .")


@dataclass
class Lesson:
    id: str
    when: str                      # key() of the phrase
    said: str                      # the phrase as first said
    calls: list[dict] = field(default_factory=list)   # [{"tool": ..., "args": {...}}]
    means: str | None = None       # or: another request ("when I say X, Y")
    taught: str = ""               # the correction / teaching sentence
    created: float = 0.0
    uses: int = 0

    def describe(self) -> str:
        if self.means:
            return f"“{self.said}” means “{self.means}”"
        what = ", ".join(_call_text(c) for c in self.calls)
        return f"“{self.said}” → {what}"


def _call_text(c: dict) -> str:
    args = ", ".join(f"{v}" for v in (c.get("args") or {}).values() if isinstance(v, (str, int, float)))
    return f"{c['tool'].replace('_', ' ')}" + (f" ({args})" if args else "")


class Lessons:
    def __init__(self):
        self.listeners: list = []
        self._items: list[Lesson] | None = None

    # --- storage (path looked up at call time, so tests can redirect it) --------------------
    def _load(self) -> list[Lesson]:
        if self._items is None:
            try:
                raw = json.loads(LESSONS_FILE.read_text(encoding="utf-8"))
                self._items = [Lesson(**d) for d in raw if isinstance(d, dict)]
            except (OSError, ValueError, TypeError):
                self._items = []
        return self._items

    def _save(self) -> None:
        items = self._load()[-MAX_LESSONS:]
        self._items = items
        LESSONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LESSONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(x) for x in items], indent=1), encoding="utf-8")
        os.replace(tmp, LESSONS_FILE)
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                pass

    def items(self) -> list[Lesson]:
        return list(self._load())

    # --- learning ---------------------------------------------------------------------------
    def learn_calls(self, said: str, calls: list[dict], taught: str) -> Lesson | None:
        k = key(said)
        calls = [c for c in calls if c.get("tool") not in NOT_ACTIONS][:4]
        if not k or not calls:
            return None
        return self._put(Lesson(secrets.token_hex(4), k, said.strip(), calls=calls, taught=taught, created=time.time()))

    def learn_meaning(self, phrase: str, means: str, taught: str) -> Lesson | None:
        k = key(phrase)
        if not k or not means.strip() or key(means) == k:
            return None
        return self._put(Lesson(secrets.token_hex(4), k, phrase.strip(), means=means.strip(), taught=taught,
                                created=time.time()))

    def _put(self, lesson: Lesson) -> Lesson:
        self._items = [x for x in self._load() if x.when != lesson.when] + [lesson]
        self._save()
        return lesson

    # --- using ------------------------------------------------------------------------------
    def match(self, text: str) -> Lesson | None:
        k = key(text)
        if not k:
            return None
        items = self._load()
        for x in reversed(items):
            if x.when == k:
                return x
        # Near misses ("open up the news" / "open the news"), but only close ones.
        best, score = None, 0.0
        for x in items:
            s = difflib.SequenceMatcher(None, x.when, k).ratio()
            if s > score:
                best, score = x, s
        return best if score >= 0.9 and len(k) >= 6 else None

    def used(self, lesson: Lesson) -> None:
        lesson.uses += 1
        self._save()

    # --- forgetting -------------------------------------------------------------------------
    def delete(self, lesson_id: str) -> bool:
        before = len(self._load())
        self._items = [x for x in self._load() if x.id != lesson_id]
        if len(self._items) != before:
            self._save()
            return True
        return False

    def forget(self, query: str | None) -> list[Lesson]:
        """'the last one' (None), or the lessons whose phrase or meaning mentions the words."""
        items = self._load()
        if not items:
            return []
        if not query or query in ("last", "that", "the last one", "it"):
            gone = [items[-1]]
        else:
            q = key(query)
            gone = [x for x in items if q and (q in x.when or q in key(x.describe()))]
        ids = {x.id for x in gone}
        self._items = [x for x in items if x.id not in ids]
        if gone:
            self._save()
        return gone


_STORE: Lessons | None = None


def get_lessons() -> Lessons:
    global _STORE
    if _STORE is None:
        _STORE = Lessons()
    return _STORE


# --- recognising what the user is doing ---------------------------------------------------------
_THING = r"(?: (?:one|website|site|page|app|song|track|video|thing|window|link|tab))?"
_NEG = re.compile(
    r"^\s*(?:(?:that'?s|it'?s|that is|it is|that was|it was)(?: the)? (?:wrong|incorrect)" + _THING + r"\b[\s,.!-]*"
    r"|that'?s not (?:it|right|what i (?:asked|meant|wanted|said)(?: for)?)\b[\s,.!-]*"
    r"|(?:you (?:did|got) (?:it|that) wrong|you (?:opened|played|did|picked|chose) the wrong (?:one|\w+))\b[\s,.!-]*"
    r"|wrong" + _THING + r"\b[\s,.!-]*"
    r"|not (?:that|this|it)(?: one)?\b[\s,.!-]*"
    r"|(?:no+|nope|nah|oops|sorry|actually|not quite|incorrect)\b[\s,.!-]*)+", re.I)
_STRONG = re.compile(r"\b(?:wrong|not (?:that|this|it|what|right|quite)|incorrect|that'?s not)\b", re.I)
_LEAD_FIX = re.compile(r"^(?:i (?:meant|mean|said|wanted|want(?: you to)?|asked(?: for)?)|it should (?:be|have been)|"
                       r"it (?:was|is)|it'?s|you should have|should be|try|instead|use|do|just)\s+", re.I)
_EXPLICIT = re.compile(r"^(?:i (?:meant|mean|said|wanted|asked)|it should|it (?:was|is)|it'?s|you should have|should be)\b", re.I)
_PLEASANTRY = re.compile(r"^(?:thanks?|thank you|that'?s (?:fine|ok|okay|all)|nevermind|never mind|all good|"
                         r"i'?m good|it'?s fine|forget it|ok|okay)?[\s.!]*$", re.I)


def explicit(text: str) -> bool:
    """A correction clear enough to learn from: "no, I meant X", "wrong one, X", "that's not
    what I asked". "Sorry, can you open Steam" or a bare "no, X" is done but never learned:
    it may just be a new request (owner: Nova started doing the wrong things)."""
    t = text.strip()
    m = _NEG.match(t)
    rest = t[m.end():] if m else t
    return bool(_EXPLICIT.match(rest) or (m and _STRONG.search(m.group(0))))


def correction(text: str) -> tuple[bool, str]:
    """(is it a correction?, the fix). 'no, I meant bbc.co.uk' -> (True, 'bbc.co.uk');
    'that's the wrong website' -> (True, ''); 'no thanks' -> (False, '')."""
    t = text.strip()
    m = _NEG.match(t)
    rest = t[m.end():] if m else t
    lead = _LEAD_FIX.match(rest)
    if not m and not (lead and re.match(r"^\s*i (?:meant|mean|said)\b", rest, re.I)):
        return False, ""
    if m and not lead and not _STRONG.search(m.group(0)) and not re.search(r"[,.!-]", m.group(0)):
        return False, ""               # "no way that's crazy": a bare "no" needs a pause or "I meant"
    if m and not lead and not _STRONG.search(m.group(0)) and not re.match(r"\s*(?:no+|nope|nah)\b", m.group(0), re.I):
        return False, ""               # "Sorry, can you open Steam" / "Actually, ..." is just a request
    fix = rest[lead.end():] if lead else rest
    fix = fix.strip(" .!,")
    if _PLEASANTRY.match(fix):
        # "no thanks", "no, it's fine": not a correction. "That's wrong." is one, without a fix.
        return (bool(_STRONG.search(t)), "")
    if m is None and not lead:
        return False, ""
    return True, fix


_TEACH = re.compile(r"^(?:from now on,?\s+)?(?:when(?:ever)?|if) i say\s+(?:"
                    r"[\"“'](?P<quoted>[^\"”']+)[\"”'],?\s+|(?P<phrase>[^,]+),\s*|(?P<bare>.+?)\s+(?=(?:i mean|that means|it means|you should|then)\s))"
                    r"(?:(?:i mean|that means|it means|you should|please|do|then)\s+)?(?P<means>.+)$", re.I)


def teaching(text: str) -> tuple[str, str] | None:
    """'when I say the news, open bbc.co.uk' -> ('the news', 'open bbc.co.uk')."""
    m = _TEACH.match(text.strip().rstrip("."))
    if not m:
        return None
    phrase = (m.group("quoted") or m.group("phrase") or m.group("bare") or "").strip(" ,\"'“”")
    means = m.group("means").strip(" ,")
    if len(phrase) < 2 or len(means) < 3:
        return None
    return phrase, means


_PREF = re.compile(
    r"^(?:(?:just so you know|fyi|by the way|btw|for (?:the|your) record)[, ]+)?(?P<s>"
    r"i (?:really |kind of |kinda |much |do )?(?:prefer|like|love|hate|dislike|don'?t like|do not like|can'?t stand|"
    r"enjoy|usually|normally|mostly|always|never) .{3,}"
    r"|my (?:favou?rite|usual|go-to|preferred|main) .{3,}"
    r"|call me .{2,}"
    r"|i'?m (?:a|an|a big|a huge) .{2,} (?:fan|player|gamer|person)"
    r"|i (?:use|play|watch|support|listen to|main) .{3,}"
    r"|from now on,? .{6,}|(?:always|never) (?:use|open|play|say|call|ask|put|give|tell|start|keep) .{3,})$", re.I)
_VAGUE = re.compile(r"\b(?:this|that|it|these|those|here|there|right now|at the moment|today)\b", re.I)


def preference(text: str) -> str | None:
    """A preference said in passing, worth remembering as is. None for questions, commands
    and things that only make sense right now ('I like this song')."""
    t = text.strip()
    if t.endswith("?") or len(t) > 200:
        return None
    m = _PREF.match(t.rstrip(".!"))
    if not m:
        return None
    s = m.group("s").strip()
    if _VAGUE.search(s) or re.match(r"^i (?:really )?(?:love|like|hate) (?:you|u|nova)\b", s, re.I):
        return None
    return s[0].upper() + s[1:]
