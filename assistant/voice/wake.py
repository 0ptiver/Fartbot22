"""Hands-free activation by name: respond only when the user says "Nova" in their sentence.

Works on the transcript (local Whisper), so no hotkeys, keyboard hooks or model training
are needed. A dedicated always-on wake-word model can replace this later.
"""

from __future__ import annotations

import re

_FILLERS = {"hey", "hi", "ok", "okay", "yo", "oi", "so", "um", "uh", "and"}
_PUNCT = re.compile(r"[^\w'\s]")


def _norm(word: str) -> str:
    return _PUNCT.sub("", word.lower()).strip("'")


def match_wake(text: str, variants: list[str], window: int = 3) -> tuple[bool, str]:
    """Is the user addressing the assistant? Returns (addressed, text without the name).

    The name must appear within the first or last `window` words, so a sentence that
    merely mentions it in the middle ("I watched a Nova documentary yesterday") is ignored.
    """
    tokens = text.split()
    norm = [_norm(t) for t in tokens]
    for variant in sorted(variants, key=lambda v: -len(v.split())):
        v = variant.lower().split()
        n = len(v)
        for i in range(0, len(norm) - n + 1):
            if norm[i:i + n] != v:
                continue
            near_start = i <= window
            near_end = i + n >= len(norm) - window + 1
            if not (near_start or near_end):
                continue
            start = i
            while start > 0 and norm[start - 1] in _FILLERS:
                start -= 1
            rest = tokens[:start] + tokens[i + n:]
            return True, _tidy(" ".join(rest))
    return False, text


def _tidy(text: str) -> str:
    text = text.lstrip(" ,.;:!?-–—").rstrip(" ,;:-–—")
    return text[:1].upper() + text[1:] if text else ""


def echo_overlap(heard: str, said: str) -> float:
    """Fraction of heard words that were in what the assistant just said (speaker echo)."""
    h = [w for w in map(_norm, heard.split()) if w]
    s = {w for w in map(_norm, said.split()) if w}
    return sum(w in s for w in h) / len(h) if h else 0.0


# --- speech without the name ------------------------------------------------------------------
# Owner: "he answers everything I say when I don't say his name". Right after a reply, or while
# Nova is talking or thinking, speech without the name only counts when it's clearly for Nova.
_FOLLOW_ON = ("and", "also", "what about", "how about", "and then", "then", "now", "actually", "wait",
              "no", "nope", "yes", "yeah", "yep", "yup", "sure", "please", "thanks", "thank you", "cheers",
              "again", "do it again", "one more", "that's", "thats", "not that", "the other one", "instead",
              "make it", "a bit", "a little", "more", "less", "louder", "quieter", "undo", "never mind", "ok",
              "okay", "perfect", "great", "good", "nice")
_UNFINISHED = {"and", "to", "the", "a", "an", "for", "with", "of", "on", "in", "my", "then", "about", "also",
               "but", "or", "so", "some", "me", "into", "at", "from", "like", "um", "uh"}
_CONTINUES = ("and", "and then", "then", "also", "to", "with", "for", "on", "in", "about", "but", "or", "please",
              "into", "at", "from", "by", "so that")


def _words(text: str) -> list[str]:
    return [w for w in (_norm(t) for t in text.split()) if w]


def _starts(words: list[str], phrases) -> bool:
    joined = " ".join(words)
    return any(joined == p or joined.startswith(p + " ") for p in phrases)


def follows_on(text: str) -> bool:
    """'And the other one', 'also turn it up', 'yes', 'thanks': talking on with Nova."""
    return _starts(_words(text), _FOLLOW_ON)


def unfinished(text: str) -> bool:
    """'Open a new tab and ...': the sentence stopped halfway (then a pause)."""
    raw = text.rstrip()
    words = _words(raw)
    return bool(words) and (words[-1] in _UNFINISHED or raw.endswith((",", "...", "…", "-")))


def continues(text: str) -> bool:
    """'... and type hello': picks up where the last sentence stopped."""
    return _starts(_words(text), _CONTINUES)
