"""Make text sound right when spoken: no markdown, URLs or maths symbols read out literally."""

from __future__ import annotations

import re

_REPLACE = [
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),                 # [text](url) -> text
    (re.compile(r"https?://\S+|www\.\S+"), "the link"),
    (re.compile(r"[*_`#]+"), ""),                                    # markdown emphasis/code/headers
    (re.compile(r"^\s*[-•]\s+", re.M), ""),                          # bullets
    (re.compile(r"(\w)²"), r"\1 squared"),
    (re.compile(r"(\w)³"), r"\1 cubed"),
    (re.compile(r"(?<=\d)\s*[x×]\s*(?=\d)"), " times "),
    (re.compile(r"\s[×*]\s"), " times "),
    (re.compile(r"\s\+\s"), " plus "),
    (re.compile(r"\s=\s"), " equals "),
    (re.compile(r"(?<=\d)\s?%"), " percent"),
    (re.compile(r"(?<=\d)\s?°\s?C\b"), " degrees Celsius"),
    (re.compile(r"(?<=\d)\s?°\s?F\b"), " degrees Fahrenheit"),
    (re.compile(r"(?<=\d)\s?°"), " degrees"),
    (re.compile(r"\s&\s"), " and "),
    (re.compile(r"\s*[—–]\s*"), ", "),
    (re.compile(r"\s{2,}"), " "),
]


def clean_for_speech(text: str) -> str:
    for pattern, repl in _REPLACE:
        text = pattern.sub(repl, text)
    return text.strip()
