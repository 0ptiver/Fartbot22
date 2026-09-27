"""Long-term memory: things the user asked Nova to remember ("my sister's birthday is June 3").

Private by design:
- stored only on this PC (data/memory.db, SQLite), never uploaded; only facts relevant to the
  current request are added to it, and only for the local model;
- only what the user explicitly asks to remember (nothing is collected behind their back);
- passwords, PINs, card and account numbers are refused (use a password manager);
- every memory can be seen and deleted by voice ("what do you remember", "forget ...") or in
  the HUD's Memory tab.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_STOP = set("a an the and or but if of to in on at for with about is are was were be been am i me my "
            "mine you your it its this that what whats when whens where who whose how do does did "
            "can could would will should please tell remember know nova sir s".split())
_SECRET = re.compile(
    r"\b(pass ?word|passcode|pin(?: code| number)?|security code|cvv|cvc|seed phrase|recovery (?:code|phrase)|"
    r"2fa|one.time code|private key|api key|secret key|social security|ssn|bank account|routing number|"
    r"sort code|card number|credit card|debit card)\b"
    r"|\b(?:\d[ -]?){13,19}\b", re.I)


class SecretRefused(ValueError):
    pass


@dataclass
class Memory:
    id: int
    text: str
    created: float


class MemoryStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.listeners: list = []                 # called with no arguments after any change (the HUD)
        self.used_listeners: list = []            # called with [ids] when memories feed a reply
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, text TEXT NOT NULL, "
                         "created REAL NOT NULL)")
        try:
            self._db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, "
                             "content='memories', content_rowid='id', tokenize='porter unicode61')")
            self.fts = True
        except sqlite3.OperationalError:               # SQLite built without FTS5
            self.fts = False
        self._db.commit()

    # --- writing ---------------------------------------------------------------------------------
    def add(self, text: str) -> tuple[Memory, bool]:
        """Store a fact. Returns (memory, replaced): a near-duplicate is updated, not repeated."""
        text = " ".join(text.split()).strip().rstrip(".")
        if not text:
            raise ValueError("Remember what?")
        if _SECRET.search(text):
            raise SecretRefused("I don't store passwords, PINs or card and account numbers. "
                                "A password manager is the safe place for those.")
        with self._lock:
            old = self._near_duplicate(text)
            now = time.time()
            if old is not None:
                self._write_fts("delete", old.id, old.text)
                self._db.execute("UPDATE memories SET text=?, created=? WHERE id=?", (text, now, old.id))
                self._write_fts("insert", old.id, text)
                self._db.commit()
                mem, replaced = Memory(old.id, text, now), True
            else:
                cur = self._db.execute("INSERT INTO memories (text, created) VALUES (?, ?)", (text, now))
                self._write_fts("insert", cur.lastrowid, text)
                self._db.commit()
                mem, replaced = Memory(cur.lastrowid, text, now), False
        self._changed()
        return mem, replaced

    def delete(self, ids: list[int]) -> list[Memory]:
        with self._lock:
            gone = [m for m in self._all() if m.id in set(ids)]
            for m in gone:
                self._write_fts("delete", m.id, m.text)
                self._db.execute("DELETE FROM memories WHERE id=?", (m.id,))
            self._db.commit()
        if gone:
            self._changed()
        return gone

    def _changed(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                pass

    def _write_fts(self, op: str, rowid: int, text: str) -> None:
        if not self.fts:
            return
        if op == "insert":
            self._db.execute("INSERT INTO memories_fts(rowid, text) VALUES (?, ?)", (rowid, text))
        else:
            self._db.execute("INSERT INTO memories_fts(memories_fts, rowid, text) VALUES('delete', ?, ?)",
                             (rowid, text))

    # --- reading ---------------------------------------------------------------------------------
    def _all(self) -> list[Memory]:
        return [Memory(*r) for r in self._db.execute("SELECT id, text, created FROM memories ORDER BY created DESC")]

    def all(self) -> list[Memory]:
        with self._lock:
            return self._all()

    def search(self, query: str, limit: int = 5) -> list[Memory]:
        words = [w for w in (x.replace("'", "") for x in re.findall(r"[a-z0-9']+", query.lower()))
                 if w not in _STOP and len(w) > 1]
        if not words:
            return []
        with self._lock:
            if self.fts:
                q = " OR ".join('"' + w.replace('"', "") + '"' for w in words)
                rows = self._db.execute(
                    "SELECT m.id, m.text, m.created FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                    "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT ?", (q, limit)).fetchall()
                return [Memory(*r) for r in rows]
            hits = [(sum(w in m.text.lower() for w in words), m) for m in self._all()]
            return [m for s, m in sorted(hits, key=lambda p: -p[0]) if s][:limit]

    def for_turn(self, user_text: str, limit: int = 5, all_if_under: int = 8) -> list[str]:
        """What to show the model this turn: everything when there's little, else what's relevant."""
        mems = self.all()
        chosen = list(reversed(mems)) if len(mems) <= all_if_under else self.search(user_text, limit)
        # "Used" (for the HUD's pulses) = the ones that actually match this request.
        used = [m.id for m in self.search(user_text, limit)] if len(mems) <= all_if_under else [m.id for m in chosen]
        for fn in list(self.used_listeners):
            try:
                fn(used)
            except Exception:
                pass
        return [m.text for m in chosen]

    def _near_duplicate(self, text: str) -> Memory | None:
        """'My sister's birthday is June 4' replaces 'my sister's birthday is June 3'."""
        from difflib import SequenceMatcher

        key = _key(text)
        best, best_r = None, 0.0
        for m in self._all():
            r = SequenceMatcher(None, key, _key(m.text)).ratio()
            if r > best_r:
                best, best_r = m, r
        if best is None:
            return None
        # Same subject, new value: "my sister's birthday is June 3" -> "... is June 4".
        # ("I like pizza" and "I like sushi" are both kept: no "is" to say it's one fact.)
        subj = _subject(key)
        same_subject = subj is not None and len(subj.split()) >= 2 and subj == _subject(_key(best.text))
        return best if best_r >= 0.92 or same_subject else None

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _key(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", text.lower()))


def _subject(key: str) -> str | None:
    m = re.match(r"^(.+?) (?:is|are|was|were)(?: called| named| on| at| in)? \S", key)
    return m.group(1) if m else None


_STORES: dict[str, MemoryStore] = {}


def get_store(settings) -> MemoryStore | None:
    """The memory store for these settings (one per file), or None when memory is off."""
    if not settings.memory.enabled:
        return None
    from assistant.core.config import ROOT
    path = Path(settings.memory.path)
    if not path.is_absolute():
        path = ROOT / path
    key = str(path)
    if key not in _STORES:
        _STORES[key] = MemoryStore(path)
    return _STORES[key]
