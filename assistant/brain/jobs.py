"""Whole jobs run in the background, so Nova keeps talking while he works. Owner: "if I tell him
to do something he actively does that thing while still being able to keep talking ... are you
still working, how far have you gotten? For him to keep working while responding, almost done sir".

Before, the job lived inside the turn that started it, so the next thing said (even "what are you
doing right now?") cancelled it, and the small model then made up "I'm still working on it"
(owner's screenshots). Now: one job at a time, owned by the brain, with its own progress that
status questions are answered from, a stop, and an announcement when it's finished.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

# What a step means, in words ("last I was ...").
_DOING = {"screenshot": "looking at the screen", "look_at_screen": "reading the screen",
          "scroll": "reading further down the page", "click_element": "clicking through it",
          "screen_click": "clicking through it", "type_text": "typing", "write_document": "writing it up",
          "press_keys": "pressing keys", "wait": "waiting for a page to load", "open_app": "opening an app",
          "my_browser": "working in your browser", "app": "working in an app", "window": "switching windows",
          "open_website": "opening a page"}
RECENT_S = 600              # a finished job is still what "how did it go?" is about for 10 minutes


@dataclass
class Job:
    task: str
    started: float = field(default_factory=time.monotonic)
    steps: int = 0
    doing: str = ""                  # the last step, in words
    note: str = ""                   # Claude's own progress note ("answered 3 of 5 questions")
    status: str = "running"          # running | done | failed | stopped
    result: str = ""
    ended: float = 0.0
    handle: asyncio.Task | None = None

    def minutes(self) -> str:
        m = int(((self.ended or time.monotonic()) - self.started) // 60)
        return "under a minute" if m < 1 else ("a minute" if m == 1 else f"{m} minutes")

    def step(self, entry: dict) -> None:
        tool = entry.get("tool", "")
        if tool == "progress":
            self.note = str((entry.get("args") or {}).get("note") or "")[:200]
            return
        self.steps += 1
        self.doing = _DOING.get(tool, self.doing or "working on it")

    def spoken(self, sir: str = "") -> str:
        """How it's going, like a person would say it."""
        if self.status == "running":
            where = self.note.rstrip(".") if self.note else (f"last I was {self.doing}" if self.doing else "just starting")
            return f"Still on it{sir}: {where}. {self.minutes().capitalize()} in so far."
        if self.status == "done":
            return f"All done{sir}. {self.result.rstrip('.')}."
        if self.status == "stopped":
            return f"I stopped{sir}, as you asked."
        return f"I had to stop{sir}: {self.result.rstrip('.')}."

    def context(self) -> str:
        """For the model's <context>: what the background job is up to."""
        state = {"running": "working on it now", "done": "finished", "failed": "stopped, stuck",
                 "stopped": "stopped by the user"}[self.status]
        bits = [f"'{self.task}'", state, f"{self.steps} steps, {self.minutes()}"]
        if self.note:
            bits.append(f"progress: {self.note}")
        if self.result:
            bits.append(f"result: {self.result}")
        return "; ".join(bits)


class Jobs:
    def __init__(self):
        self.current: Job | None = None

    def running(self) -> Job | None:
        j = self.current
        return j if j is not None and j.status == "running" else None

    def recent(self) -> Job | None:
        j = self.current
        if j is None:
            return None
        if j.status == "running" or time.monotonic() - j.ended < RECENT_S:
            return j
        return None

    def start(self, job: Job, work) -> Job:
        """work: a coroutine function taking the job; it sets status/result itself."""
        self.current = job
        job.handle = asyncio.ensure_future(work(job))
        job.handle.add_done_callback(lambda t: t.cancelled() or t.exception())
        return job

    def stop(self) -> Job | None:
        job = self.running()
        if job is None:
            return None
        job.status, job.ended = "stopped", time.monotonic()
        if job.handle is not None and not job.handle.done():
            job.handle.cancel()
        return job


STATUS = re.compile(
    r"\b(?:are you (?:still )?(?:working|doing it|on it|busy|going|at it|done|finished)|how(?:'?s| is) it (?:going|coming)"
    r"|how(?:'?s| is) (?:the|my) (?:assignment|homework|task|job|work|survey|essay|form|quiz)(?: going| coming)?"
    r"|how far|(?:any|what'?s the|how'?s the) progress|status update|did you finish|have you finished|is it done|what are you (?:doing|working on|up to)"
    r"|what(?:'?s| is) (?:happening|going on)|how (?:much )?longer|still going|nearly done|almost done"
    r"|you (?:aren'?t|are not|'re not) doing anything|are you (?:still )?there)\b", re.I)
STOP = re.compile(
    r"^\W*(?:nova[, ]+)?(?:(?:ok|okay|please|just|actually|alright)[, ]+)*(?:stop|halt|cancel|abort|quit|pause)"
    r"(?:[, ]+(?:it|that|working|now|please|nova|sir|the (?:job|task|assignment|work|homework)|what you'?re doing))*\W*$",
    re.I)
