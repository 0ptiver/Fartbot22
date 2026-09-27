"""Spoken control words: yes/no for confirmations, and the kill switch."""

from __future__ import annotations

import re

_YES = {"yes", "yeah", "yep", "yup", "sure", "affirmative", "correct", "confirm", "confirmed",
        "ok", "okay", "please", "absolutely", "certainly", "definitely", "aye"}
_YES_PHRASES = ("do it", "go ahead", "go for it", "please do", "yes please", "that's right",
                "sounds good", "make it so", "carry on", "proceed")
_NO = {"no", "nope", "nah", "negative", "cancel", "don't", "dont", "stop", "wait", "abort"}
_NO_PHRASES = ("don't do it", "never mind", "nevermind", "hold on", "not now", "no thanks",
               "leave it", "forget it", "do not")

_STAND_DOWN = re.compile(r"\b(?:stand down|stop everything|go to sleep|stop listening|shut up and stop)\b")
_RESUME = re.compile(r"\b(?:wake up|stand up|resume|i need you|back to work|you can listen|start listening)\b")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z' ]+", " ", text.lower().replace("’", "'")).strip()


def classify_yes_no(text: str) -> bool | None:
    """True (yes), False (no) or None (unclear). A 'no' anywhere wins: safety first."""
    t = _norm(text)
    words = set(t.split())
    if any(p in t for p in _NO_PHRASES) or words & _NO:
        return False
    if any(p in t for p in _YES_PHRASES) or words & _YES:
        return True
    return None


_CANCEL = re.compile(
    r"^(?:(?:hey |ok |okay |no |wait |actually |oh |sorry |um |uh |nova )*)"
    r"(?:cancel|cancel (?:that|it|this|the last (?:one|thing)|what you just did)|never ?mind|forget (?:it|that)|abort|"
    r"scrap that|undo that|undo|stop that|don'?t do (?:that|it)|call it off|take that back)"
    r"(?: please| nova| sir| thanks| then| now)*$")


def is_cancel(text: str) -> bool:
    """'Cancel', 'never mind', 'cancel that', 'Nova, forget it', 'undo that'."""
    return bool(_CANCEL.match(" ".join(_norm(text).split())))


def is_stand_down(text: str) -> bool:
    return bool(_STAND_DOWN.search(_norm(text)))


def is_resume(text: str) -> bool:
    return bool(_RESUME.search(_norm(text)))
