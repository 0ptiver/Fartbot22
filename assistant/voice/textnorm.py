"""Normalize spoken/transcribed text so "90 x 90", "ninety times ninety" and "90 times 90"
compare equal. Used to recognise Nova's own voice coming back through the speakers."""

from __future__ import annotations

import re

_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate(
    "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 10**6, "billion": 10**9}
_SYMBOLS = {"x": "times", "×": "times", "*": "times", "+": "plus", "=": "equals", "%": "percent",
            "&": "and", "/": "over", "²": "squared", "³": "cubed", "-": "minus"}
_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?|[a-z']+|[×*+=%&/²³]|(?<=\s)-(?=\s)")


def _words_to_numbers(tokens: list[str]) -> list[str]:
    out, total, current, in_number = [], 0, 0, False

    def flush():
        nonlocal total, current, in_number
        if in_number:
            out.append(str(total + current))
        total, current, in_number = 0, 0, False

    for t in tokens:
        if t in _UNITS:
            current += _UNITS[t]
            in_number = True
        elif t in _TENS:
            current += _TENS[t]
            in_number = True
        elif t in _SCALES and in_number:
            if t == "hundred":
                current *= 100
            else:
                total += current * _SCALES[t]
                current = 0
        elif t == "and" and in_number:
            continue
        else:
            flush()
            out.append(t)
    flush()
    return out


def normalize_words(text: str) -> list[str]:
    tokens = _TOKEN.findall(text.lower().replace("’", "'"))
    tokens = [_SYMBOLS.get(t, t) for t in tokens]
    tokens = [t.replace(",", "") if t[0].isdigit() else t.strip("'") for t in tokens]
    return [t for t in _words_to_numbers(tokens) if t]


def compact(text: str) -> str:
    return "".join(normalize_words(text))
