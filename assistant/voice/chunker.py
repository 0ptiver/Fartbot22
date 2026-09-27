"""Split streamed LLM text into speakable chunks as early as possible."""

from __future__ import annotations

import re

_ABBREV = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "approx", "no", "jr", "sr"}
_SENT_END = re.compile(r"[.!?…]+[\"')\]]*\s")
_CLAUSE_END = re.compile(r"[,;:—–]\s")


class SentenceChunker:
    """Feed text deltas; get back chunks ready for TTS.

    The first chunk may end at a clause (comma etc.) so speech starts sooner;
    later chunks end at sentence boundaries so prosody stays natural.
    """

    def __init__(self, first_chunk_min_chars: int = 12, max_chars: int = 220,
                 honorific: str = "sir"):
        self.first_min = first_chunk_min_chars
        self.honorific = honorific.lower()
        self.max_chars = max_chars
        self.buf = ""
        self.emitted = 0

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out: list[str] = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            chunk, self.buf = self.buf[:cut].strip(), self.buf[cut:].lstrip()
            if chunk:
                out.append(chunk)
                self.emitted += 1
        return out

    def flush(self) -> list[str]:
        chunk, self.buf = self.buf.strip(), ""
        if chunk:
            self.emitted += 1
            return [chunk]
        return []

    def _find_cut(self) -> int | None:
        for m in _SENT_END.finditer(self.buf):
            end = m.end()
            if self._is_false_sentence_end(m.start()):
                continue
            if self.emitted == 0 or end >= 4:
                return end
        if self.emitted == 0:
            for m in _CLAUSE_END.finditer(self.buf):
                if m.end() < self.first_min:
                    continue
                # Wait for the next word: "One moment, sir." must not become "One moment," + "sir."
                nxt = re.match(r"\s*([\w']+)(\W|$)", self.buf[m.end():])
                if nxt is None or nxt.group(2) == "" :
                    return None
                if self.honorific and nxt.group(1).lower() == self.honorific:
                    continue
                return m.end()
        if len(self.buf) > self.max_chars:
            # Run-on text: break at the last space so TTS never waits too long.
            space = self.buf.rfind(" ", 0, self.max_chars)
            return space + 1 if space > 0 else self.max_chars
        return None

    def _is_false_sentence_end(self, dot: int) -> bool:
        if self.buf[dot] != ".":
            return False
        word = re.findall(r"[\w.]+$", self.buf[:dot])
        w = word[0].lower() if word else ""
        if w in _ABBREV:
            return True
        if len(w) == 1 and w.isalpha():  # initials: "J. R. R."
            return True
        # decimals like 3.5 never reach here (no whitespace after the dot)
        return False
