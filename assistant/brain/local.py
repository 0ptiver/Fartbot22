"""Conversation on a free local model served by Ollama (brain.backend: local).

Same event stream as the Claude API brain, so the voice loop and clients don't
care which one is running. Hard tasks go to the expert (your Claude subscription
via Claude Code, by default) through the `escalate` tool.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, AsyncIterator

import logging

import httpx

from assistant.brain.expert import Expert, ExpertError, create_expert
from assistant.brain.intents import match_intent
from assistant.brain.llm import (BrainError, Event, TextDelta, ToolFinished, ToolStarted,
                                 TurnComplete, _ms)
from assistant.brain.prompts import system_prompt, turn_context
from assistant.core.config import Settings
from assistant.core.conversation import Conversation
from assistant.tools.registry import ToolContext, ToolRegistry, ToolResult, _summarize_content
from assistant.tools.routines import match_routine


log = logging.getLogger(__name__)


class OllamaError(Exception):
    pass


class ThinkFilter:
    """Keeps a model's reasoning out of speech.

    - `<think>...</think>` at the start of the reply is dropped.
    - hold=True (a model that can't turn thinking off and doesn't emit the opening
      tag): nothing is released until `</think>` arrives; if it never does, the
      whole reply is released at the end.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self, hold: bool = False):
        self.state = "hold" if hold else "start"
        self.buf = ""
        self._lstrip = False                    # trim blank lines right after </think>

    def feed(self, text: str) -> str:
        self.buf += text
        if self.state == "start":
            head = self.buf.lstrip()
            if not head or (len(head) < len(self.OPEN) and self.OPEN.startswith(head)):
                return ""                       # could still be the start of <think>
            if head.startswith(self.OPEN):
                self.state, self.buf = "in", head[len(self.OPEN):]
            else:
                self.state = "pass"
        if self.state in ("in", "hold"):
            idx = self.buf.find(self.CLOSE)
            if idx < 0:
                return ""
            self.state, self.buf = "pass", self.buf[idx + len(self.CLOSE):]
            self._lstrip = True
        out, self.buf = self.buf, ""
        if self._lstrip:
            out = out.lstrip()
            self._lstrip = not out
        return out.replace(self.OPEN, "").replace(self.CLOSE, "")

    def flush(self) -> str:
        out, self.buf = self.buf, ""
        if self.state == "in":
            return ""                           # unterminated reasoning: never speak it
        return out.replace(self.OPEN, "").replace(self.CLOSE, "").strip() if self.state != "pass" else out


class ContextFilter:
    """Never shows or says the <context> block. The small model sometimes copies its notes (the
    time, the window in front, remembered facts) into its reply (owner's case on the phone).
    Works on a stream: text that could be the start of a tag is held until it's clear."""

    OPEN, CLOSE = "<context>", "</context>"

    def __init__(self):
        self.buf, self.inside, self._lstrip = "", False, False

    def feed(self, text: str) -> str:
        self.buf += text
        out = []
        while True:
            if self.inside:
                i = self.buf.find(self.CLOSE)
                if i < 0:
                    self.buf = self.buf[-(len(self.CLOSE) - 1):]    # keep only what could be the close
                    break
                self.buf, self.inside, self._lstrip = self.buf[i + len(self.CLOSE):], False, True
                continue
            i = self.buf.find(self.OPEN)
            j = self.buf.find(self.CLOSE)
            if j >= 0 and (i < 0 or j < i):                        # a stray close: drop what came before
                self.buf, self._lstrip = self.buf[j + len(self.CLOSE):], True
                out.clear()
                continue
            if i >= 0:
                out.append(self.buf[:i])
                self.buf, self.inside = self.buf[i + len(self.OPEN):], True
                continue
            keep = self._partial()
            out.append(self.buf[:len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:]
            break
        text = "".join(out)
        if self._lstrip:
            text = text.lstrip()
            self._lstrip = not text
        return text

    def _partial(self) -> int:
        """How much of the end could be the start of a tag."""
        for n in range(min(len(self.buf), len(self.CLOSE) - 1), 0, -1):
            tail = self.buf[-n:]
            if self.OPEN.startswith(tail) or self.CLOSE.startswith(tail):
                return n
        return 0

    def flush(self) -> str:
        out, self.buf = ("" if self.inside else self.buf), ""
        self.inside = False
        return out.lstrip() if self._lstrip else out


class LocalBrain:
    def __init__(self, settings: Settings, registry: ToolRegistry,
                 http: httpx.AsyncClient | None = None, expert: Expert | None = None):
        self.settings = settings
        self.cfg = settings.brain.local
        self.registry = registry
        self.http = http or httpx.AsyncClient(base_url=self.cfg.host,
                                              timeout=httpx.Timeout(self.cfg.timeout_s, connect=3))
        self.expert = expert or create_expert(settings)
        self._system = system_prompt(settings, local=True)
        self._think_supported = True
        self._hold_think = False               # set for models that can't stop thinking
        self.last_vision_stats: dict = {}

    # --- helpers ----------------------------------------------------------------
    def tools(self) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {
                    "name": d["name"], "description": d["description"],
                    "parameters": d["input_schema"]}}
                for d in self.registry.definitions()]

    def _body(self, messages: list[dict], tools: bool = True, model: str | None = None) -> dict:
        body: dict[str, Any] = {
            "model": model or self.cfg.model,
            "messages": [{"role": "system", "content": self._system}, *messages],
            "stream": True,
            "keep_alive": self.cfg.keep_alive,
            "options": {"num_ctx": self.cfg.num_ctx, "temperature": self.cfg.temperature},
        }
        if tools:
            body["tools"] = self.tools()
        if self._think_supported:
            body["think"] = self.cfg.think
        return body

    def _fitted(self, messages: list[dict], allow_tools: bool) -> list[dict]:
        """The history cut to what fits the model's window (see brain/fit.py)."""
        from assistant.brain import fit
        # Measured with Qwen's tokenizer: prose ~4.3 characters a token, the tool schemas ~3.6.
        tools = len(json.dumps(self.tools(), ensure_ascii=False)) / 3.5 if allow_tools else 0
        fixed = int(len(self._system) / 4.2 + tools) + 150          # + the chat template's own tokens
        return fit.fit(messages, max(1000, self.cfg.num_ctx - fit.REPLY_TOKENS - fixed))

    async def capabilities(self, model: str | None = None) -> list[str]:
        r = await self.http.post("/api/show", json={"model": model or self.cfg.model})
        return r.json().get("capabilities", []) if r.status_code == 200 else []

    async def warm_up(self) -> None:
        """Load the model *with the same settings real turns use* and pre-process the
        system prompt + tool list, so Ollama's prompt cache makes the first reply fast.
        (A different num_ctx than the real request would force a full model reload.)"""
        body = self._body([{"role": "user", "content": "hi"}])
        body["options"] = {**body["options"], "num_predict": 1}
        for _ in range(2):  # second try only if the model rejects think=false
            try:
                async for _chunk in self._stream(body):
                    pass
                return
            except _RetryWithoutThink:
                body = self._body([{"role": "user", "content": "hi"}])
                body["options"] = {**body["options"], "num_predict": 1}
            except httpx.ConnectError as e:
                raise OllamaError("Ollama isn't running. Start the Ollama app.") from e

    async def _stream(self, body: dict) -> AsyncIterator[dict]:
        async with self.http.stream("POST", "/api/chat", json=body) as r:
            if r.status_code != 200:
                detail = (await r.aread()).decode("utf-8", "replace")
                if r.status_code == 400 and "think" in detail and "think" in body:
                    if body["model"] == self.cfg.model:
                        # Chat model rejects think=false: it may reason in its reply, so filter it.
                        self._think_supported = False
                        self._hold_think = True
                    raise _RetryWithoutThink()
                if r.status_code == 404:
                    raise OllamaError(f"Model {body['model']} isn't downloaded. "
                                      f"Run: ollama pull {body['model']}")
                raise OllamaError(f"Ollama error {r.status_code}: {detail[:200]}")
            async for line in r.aiter_lines():
                if line.strip():
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise OllamaError(chunk["error"])
                    yield chunk

    # --- working it out when stuck (brain/agent.py) ------------------------------------------
    async def run_turn(self, conv: Conversation, user_text: str, ctx: ToolContext,
                       extra_context: dict[str, str] | None = None) -> AsyncIterator[Event]:
        """Everything below, plus: when Nova can't do something (a PC action failed, or the model
        says it can't), Claude works it out with Nova's tools and the steps are learned. 'Figure it
        out' / 'try another way' asks for that directly (owner chose automatic: "it keeps saying I
        can't do this and that ... allow it to self learn and be able to work through things")."""
        who = self.settings.assistant.address_user_as
        sir = f", {who}" if who else ""
        if _WORK_IT_OUT.match(user_text.strip()) and self._can_rescue(ctx):
            task = getattr(conv, "last_request", None) or user_text
            async for ev in self._work_it_out(conv, task, ctx, sir, "Oliver asked Nova to work it out itself."):
                yield ev
            return
        conv.last_request = user_text
        async for ev in self._rescued(conv, user_text, self._learning_turn(conv, user_text, ctx, extra_context),
                                      ctx, sir):
            yield ev

    def _agent(self):
        from assistant.brain.agent import Agent
        from assistant.brain.expert import ClaudeCodeExpert
        if getattr(self, "_agent_obj", None) is None:
            self._agent_obj = Agent(self.settings, self.expert if isinstance(self.expert, ClaudeCodeExpert) else None)
        return self._agent_obj

    def _can_rescue(self, ctx: ToolContext) -> bool:
        # From the phone only when PC control from the phone is allowed, and then after a Yes on
        # the phone (_work_it_out asks): Claude controls the PC.
        if ctx.remote and (self.settings.phone.pc_control != "ask" or ctx.confirm is None):
            return False
        return self.settings.brain.agent.enabled and self._agent().available()

    async def _rescued(self, conv: Conversation, request: str, gen, ctx: ToolContext, sir: str) -> AsyncIterator[Event]:
        """Pass a turn through, but never let it claim or give up falsely:
        - a PC action failed (and nothing fixed it), or Nova said it couldn't do an action;
        - Nova said it did something ("Skipped.", "Closed Spotify", "Now on YouTube") while nothing
          actually happened this turn (owner's screenshots: those were all made up).
        Then Claude works it out (when on), or Nova says plainly that it hasn't done it."""
        held: list[Event] = []
        failing = False
        claimed = False
        refused = False                          # the model said no to the request itself
        did = False                              # an action really happened this turn
        failed_why = ""
        rescue = self.settings.brain.agent.on_failure and self._can_rescue(ctx)
        question = bool(_QUESTION.match(request))
        asks = bool(_ACTION_REQUEST.search(request) or _MORE_ACTIONS.search(request)) and not question
        first: list[Event] = []                 # the reply's first sentence, until it's clear it's honest

        def verdict(text: str) -> str | None:
            if not did and _HELP_REFUSAL.search(text):
                return "refused"                  # said no to the request itself: Claude answers it
            if asks and (_STUCK in text or _CANT.search(text)):
                return "stuck"
            if not did and not question and (
                    _DOING_CLAIM.search(text.strip()) or _DONE_CLAIM.search(text)
                    or ((asks or _ABOUT_NOVA.search(request)) and _STATE_CLAIM.search(text))):
                return "claim"
            return None

        def made_up(sentence: str) -> bool:
            """A later sentence claiming an action nothing did (owner's case: a question got a list
            of remembered facts, then "Opening business.facebook.com in your browser, sir.")."""
            s = sentence.strip()
            return not did and bool(_DOING_CLAIM.search(s) or (not question and _DONE_CLAIM.search(s)))

        pending = ""                             # the sentence being streamed, after the first
        dropped: list[str] = []
        shown = False                            # any text reached the user

        def sentences(final: bool) -> list[TextDelta]:
            nonlocal pending, shown, failing, failed_why, refused
            out = []
            while pending:
                m = re.search(r"[.!?](?:\s+|$)", pending)
                if m and (m.end() < len(pending) or final):
                    end = m.end()
                elif final:
                    end = len(pending)
                else:
                    break                        # the sentence isn't finished yet
                sent, pending = pending[:end], pending[end:]
                if made_up(sent):
                    log.info("dropped a made-up claim: %s", sent.strip()[:120])
                    dropped.append(sent)
                    continue
                if not did and _HELP_REFUSAL.search(sent):
                    failing, refused = True, True        # "Sure. Actually, I can't help with that."
                    held.append(TextDelta(sent + pending))
                    pending = ""
                    break
                if asks and not did and (_STUCK in sent or _CANT.search(sent)):
                    # "Certainly, sir. Unfortunately I can't close that.": the rest is held and the
                    # job goes to Claude (or is said as it is, when Claude is off).
                    failing, failed_why = True, failed_why or f"Nova's everyday model said: {sent.strip()[:150]}"
                    held.append(TextDelta(sent + pending))
                    pending = ""
                    break
                if sent.strip():
                    shown = True
                out.append(TextDelta(sent))
            return out

        def judge(text: str) -> None:
            nonlocal failing, claimed, failed_why, refused
            v = verdict(text)
            if v == "refused":
                failing = refused = True
                return
            if v:
                failing, claimed = True, claimed or v == "claim"
                failed_why = failed_why or (f"Nova's everyday model said '{text.strip()[:120]}' without doing anything."
                                            if v == "claim" else f"Nova's everyday model said: {text.strip()[:150]}")
        classified = False
        async for ev in gen:
            if not classified and isinstance(ev, TextDelta) and not failing:
                first.append(ev)
                text = "".join(e.text for e in first)
                if re.search(r"[.!?](?:\s|$)", text) or len(text.split()) >= 25:
                    classified = True
                    clean = _LEAKED_CALL.sub("", text)        # ">window close" printed by the model
                    judge(clean)
                    if clean != text:
                        first = [TextDelta(clean)] if clean.strip() else []
                    if failing:
                        held.extend(first)
                    elif made_up(clean):             # e.g. a question answered with "Opening X."
                        dropped.append(clean)
                    else:
                        shown = shown or bool(clean.strip())
                        for e in first:
                            yield e
                    first = []
                continue
            if first and not isinstance(ev, TextDelta):
                if isinstance(ev, TurnComplete):
                    judge("".join(e.text for e in first))
                if failing:
                    held.extend(first)
                else:
                    pending += "".join(e.text for e in first)
                first, classified = [], True
            if isinstance(ev, TextDelta) and not failing and classified:
                pending += ev.text
                for e in sentences(False):
                    yield e
                continue
            if not isinstance(ev, TextDelta) and pending and not failing:
                for e in sentences(True):
                    yield e
            if isinstance(ev, TurnComplete) and not failing and dropped:
                for d in dropped:                     # neither shown nor remembered as said
                    ev.text = ev.text.replace(d.strip(), "").strip()
                    last = conv.messages[-1] if conv.messages else {}
                    if last.get("role") == "assistant" and isinstance(last.get("content"), str):
                        last["content"] = last["content"].replace(d.strip(), "").strip()
                if not shown:
                    honest = f"Sorry{sir}, I haven't actually done that. Could you say it another way?"
                    yield TextDelta(honest)
                    ev.text = honest
            if isinstance(ev, ToolFinished) and not ev.is_error and ev.name != "work_it_out":
                did = True
            if isinstance(ev, ToolFinished) and ev.name in _RESCUABLE:
                if ev.is_error and not ev.summary.startswith(_DELIBERATE):
                    failing, failed_why = True, ev.summary
                elif not ev.is_error and failing and not claimed:   # the model sorted it out itself
                    failing = False
                    for h in held:
                        yield h
                    held = []
            if isinstance(ev, TextDelta) and failing:
                held.append(ev)
                continue
            if isinstance(ev, TurnComplete) and failing and refused:
                answered = False
                async for e in self._answer_instead(conv, request, ctx):
                    answered = True
                    yield e
                if answered:
                    return
                for h in held:                    # Claude isn't there: say what the model said
                    yield h
                held = []
                yield ev
                return
            if isinstance(ev, TurnComplete) and failing:
                if rescue:
                    note = f"What Nova tried first failed: {failed_why}" if failed_why else ""
                    async for e in self._work_it_out(conv, request, ctx, sir, note):
                        yield e
                    return
                if claimed:
                    honest = f"Sorry{sir}, I haven't actually done that. Could you say it another way?"
                    yield TextDelta(honest)
                    ev.text = honest
                    yield ev
                    return
                for h in held:
                    yield h
                held = []
            yield ev
        for h in held:
            yield h

    async def _answer_instead(self, conv: Conversation, request: str, ctx: ToolContext) -> AsyncIterator[Event]:
        """The everyday model refused the owner's own request ("I can't help with that"): Claude
        answers it instead, and Nova says that answer. Yields nothing if Claude can't be reached."""
        if "escalate" not in self.registry._tools:
            return
        t0 = time.perf_counter()
        started = time.perf_counter()
        res = await self.registry.execute("escalate", {"task": request}, ctx)
        text = res.content if isinstance(res.content, str) else _summarize_content(res.content, 2000)
        if res.is_error or not text.strip():
            log.info("the model refused and Claude couldn't answer: %s", text[:200])
            return
        log.info("the everyday model refused %r; Claude answered instead", request[:80])
        yield ToolStarted("answer_1", "escalate", {"task": request})
        yield ToolFinished("answer_1", "escalate", False, _summarize_content(text, 200), _ms(started))
        answer = text.strip()
        last = conv.messages[-1] if conv.messages else {}
        if last.get("role") == "assistant":                # remember the answer, not the refusal
            last["content"] = answer
        else:
            conv.messages.append({"role": "assistant", "content": answer})
        yield TextDelta(answer)
        yield TurnComplete(answer, {"total_ms": _ms(t0)}, {"input_tokens": 0, "output_tokens": 0}, "answered")

    async def _work_it_out(self, conv: Conversation, task: str, ctx: ToolContext, sir: str,
                           note: str = "") -> AsyncIterator[Event]:
        from assistant.brain import lessons as L
        from assistant.brain.agent import AgentResult, learnable
        from assistant.brain.situation import situation
        t0 = time.perf_counter()
        if ctx.remote:
            ok = await ctx.confirm("work_it_out", {"task": task})
            if not ok:
                say = f"All right{sir}, I'll leave it."
                yield TextDelta(say)
                yield TurnComplete(say, {"total_ms": _ms(t0)}, {"input_tokens": 0, "output_tokens": 0}, "agent")
                return
        yield TextDelta(f"Let me work that out{sir}. ")
        yield ToolStarted("agent", "work_it_out", {"task": task})
        try:
            now = await situation()
        except Exception:
            now = {}
        context = "\n".join([note] + [f"{k}: {v}" for k, v in now.items()]).strip()
        steps: asyncio.Queue = asyncio.Queue()
        job = asyncio.ensure_future(self._agent().run(task, context, steps.put_nowait))
        n = 0
        try:
            while True:
                getter = asyncio.ensure_future(steps.get())
                done, _ = await asyncio.wait({job, getter}, return_when=asyncio.FIRST_COMPLETED)
                entries = [getter.result()] if getter in done else []
                if getter not in done:
                    getter.cancel()
                if job in done:
                    while not steps.empty():
                        entries.append(steps.get_nowait())
                for e in entries:                       # Claude's steps, live in the feed
                    n += 1
                    yield ToolStarted(f"agent_{n}", e.get("tool", "?"), e.get("args") or {})
                    yield ToolFinished(f"agent_{n}", e.get("tool", "?"), not e.get("ok"),
                                       str(e.get("result", ""))[:200], int(e.get("ms") or 0))
                if job in done:
                    break
            result = job.result()
        except asyncio.TimeoutError:
            result = AgentResult(False, "I ran out of time working that out.")
        except ExpertError as e:
            result = AgentResult(False, str(e))
        finally:
            if not job.done():
                job.cancel()
        yield ToolFinished("agent", "work_it_out", not result.ok, result.say[:200], _ms(t0))
        say = result.say.rstrip(".") + "."
        if result.ok and self.settings.brain.agent.learn:
            calls = learnable(result.steps)
            if calls and L.get_lessons().learn_calls(task, calls, "worked out by Claude"):
                say += " I've learned how, so next time it's instant."
        conv.messages.append({"role": "user", "content": task})
        conv.messages.append({"role": "assistant", "content": say})
        conv.trim()
        conv.last_action = {"text": task, "at": time.time()}
        yield TextDelta(say)
        yield TurnComplete(say, {"total_ms": _ms(t0)}, {"input_tokens": 0, "output_tokens": 0}, "agent")

    # --- learning from the user (brain/lessons.py) ----------------------------------------
    async def _learning_turn(self, conv: Conversation, user_text: str, ctx: ToolContext,
                             extra_context: dict[str, str] | None = None) -> AsyncIterator[Event]:
        """Every request: corrections ("no, I meant ..."), teaching ("when I say X, Y"),
        preferences said in passing, and things learned before, then the request itself.
        A request that fixes a mistake is remembered for next time."""
        from assistant.brain import lessons as L

        ctx.services.setdefault("brain", self)
        store = L.get_lessons()
        who = self.settings.assistant.address_user_as
        sir = f", {who}" if who else ""
        last = getattr(conv, "last_action", None)
        recent = last if last and time.time() - last["at"] < L.FIX_WINDOW_S else None
        awaiting = getattr(conv, "awaiting_fix", None)
        conv.awaiting_fix = None
        original: str | None = None
        learn = True
        request = user_text
        told_off = _prohibition(user_text)
        if told_off is not None:
            # "Stop taking me to that website", "don't do that again": never an action. The small
            # model re-ran its last call for each of these (owner: "it opened this scam website
            # like 50 times").
            conv.awaiting_fix = None
            conv.last_action = None
            async for ev in self._say(conv, user_text, told_off.format(sir=sir)):
                yield ev
            return
        is_fix, fix = L.correction(user_text)
        if awaiting:
            if _NEVER_MIND.match(user_text):
                async for ev in self._say(conv, user_text, f"All right{sir}."):
                    yield ev
                return
            original, request = awaiting, (fix if is_fix and fix else user_text)
        elif is_fix:
            if recent is None:
                request = fix or user_text           # "I meant ten minutes": nothing to correct, just do it
            elif not fix:
                conv.awaiting_fix = recent["text"]
                async for ev in self._say(conv, user_text, f"Sorry{sir}. What should I have done?"):
                    yield ev
                return
            else:
                original, request = recent["text"], fix
                learn = L.explicit(user_text)
                if recent.get("lesson"):              # a learned shortcut was wrong: drop it
                    store.delete(recent["lesson"])
        else:
            taught = L.teaching(user_text)
            if taught:
                lesson = store.learn_meaning(taught[0], taught[1], user_text)
                reply = (f"Got it. When you say “{taught[0]}”, I'll {taught[1]}." if lesson
                         else "I couldn't learn that one.")
                async for ev in self._say(conv, user_text, reply):
                    yield ev
                return
            missed = self._complaint(user_text, ctx)
            if missed:
                # "You didn't close my video and you didn't open Spotify!": do those, don't apologise.
                async for ev in self._run_calls(conv, user_text, missed, ctx):
                    yield ev
                conv.last_action = {"text": user_text, "at": time.time()}
                return
            small = _small_talk(user_text, sir, self.settings.assistant.owner_name, self.settings.assistant.name)
            if small:
                async for ev in self._say(conv, user_text, small):
                    yield ev
                return
            pref = L.preference(user_text)
            if pref:
                async for ev in self._say(conv, user_text, await self._note_preference(pref, ctx, sir)):
                    yield ev
                return
            parts = self._compound(user_text, ctx)
            if parts:
                async for ev in self._run_calls(conv, user_text, parts, ctx):
                    yield ev
                conv.last_action = {"text": user_text, "at": time.time()}
                return
            browse = _open_browser_followup(user_text, getattr(conv, "last_query", None))
            if browse is not None:
                async for ev in self._run_calls(conv, user_text, [browse], ctx):
                    yield ev
                return
            lesson = store.match(user_text)
            if lesson is not None:
                store.used(lesson)
                if lesson.means:
                    request = lesson.means
                else:
                    async for ev in self._replay(conv, user_text, lesson, ctx):
                        yield ev
                    return
        if original is not None and not self._fast_path(request, ctx):
            # A fragment ("the BBC one"): the model gets it with what it corrects.
            request = f'{user_text}\n(This corrects my previous request, "{original}". Do what I actually meant now.)'
        calls: list[dict] = []
        spoke = False
        async for ev in self._turn(conv, request, ctx, extra_context):
            if isinstance(ev, TextDelta) and ev.text.strip():
                spoke = True
            if isinstance(ev, TurnComplete) and not spoke:
                # Never silent (owner: "it just doesn't do it or respond"): the model said nothing.
                ok = [c for c in calls if c["ok"]]
                filler = ("Done." if ok else f"Sorry{sir}, that didn't work." if calls
                          else f"Sorry{sir}, I didn't catch that. Could you say it again?")
                yield TextDelta(filler)
                ev.text = filler
            if isinstance(ev, ToolStarted):
                calls.append({"tool": ev.name, "args": ev.input, "ok": None})
            elif isinstance(ev, ToolFinished):
                for c in reversed(calls):
                    if c["tool"] == ev.name and c["ok"] is None:
                        c["ok"] = not ev.is_error
                        break
            elif isinstance(ev, TurnComplete) and original is not None and learn:
                done = [{"tool": c["tool"], "args": c["args"]} for c in calls if c["ok"]]
                if store.learn_calls(original, done, user_text):
                    note = " Noted for next time."
                    yield TextDelta(note)
                    ev.text = (ev.text + note).strip()
            yield ev
        for c in calls:                                  # what was just looked up, for "open Edge and look"
            a = c.get("args") or {}
            q = a.get("query") if c["tool"] == "web_search" else a.get("search") if c["tool"] == "open_website" \
                else a.get("text") if c["tool"] == "browser" and a.get("action") == "search" else None
            if q:
                conv.last_query = q
        if any(c["ok"] and c["tool"] not in L.NOT_ACTIONS for c in calls):
            conv.last_action = {"text": original or user_text, "at": time.time()}

    def _compound(self, text: str, ctx: ToolContext) -> list[dict] | None:
        """'Pause the video and open Spotify' -> two direct commands, no model (owner's case: the
        model said 'Sorry, I wasn't able to do that'). 'Open Steam and Discord' reuses the verb.
        Only when every part is a known command; otherwise the model gets the whole thing."""
        from assistant.brain.intents import _clean
        t = _clean(text)
        parts = [p.strip(" .,!?") for p in _SPLIT.split(t) if p and p.strip(" .,!?")]
        if not 2 <= len(parts) <= 3:
            return None
        grid = ctx.services.get("grid")
        flags = dict(grid_visible=bool(grid and grid.grid is not None), labels=bool(grid and grid.labels is not None),
                     browser_active=_browser_active())
        whole = match_intent(text, **flags)
        if whole and (whole[0] in ("browser", "set_reminder", "set_alarm")
                      or (whole[0] == "open_website" and whole[1].get("search"))):
            return None       # "open kbb.com and search for a civic", "open youtube and search for lofi" are one thing
        calls, verb = [], ""
        for p in parts:
            it = match_intent(p, **flags)
            if it is None and verb:
                it = match_intent(f"{verb} {p}", **flags)     # "open steam and discord"
            if it is None:
                return None
            verb = p.split()[0] if p.split()[0] in ("open", "close", "launch", "start", "quit", "play", "pause") else verb
            calls.append({"tool": it[0], "args": it[1]})
        if whole and whole[0] == "video" and all(c["tool"] in ("video", "media") for c in calls):
            return None                                  # "full screen the video and play it": one video command
        # "Open a new tab for me? Can you open Kelley Blue Book?": one new tab with the site in it.
        if (len(calls) == 2 and calls[0]["tool"] == "my_browser" and calls[0]["args"] == {"action": "new_tab"}
                and calls[1]["tool"] == "open_website" and calls[1]["args"].get("site")):
            return [{"tool": "my_browser", "args": {"action": "new_tab", "go": calls[1]["args"]["site"]}}]
        return calls

    def _complaint(self, text: str, ctx: ToolContext) -> list[dict] | None:
        """The things the user says Nova didn't do, as direct commands (all of them known, or None)."""
        clauses = _DIDNT.findall(text)
        if not clauses:
            return None
        # Other sentences said with it ("You didn't pause it. Just go ahead and close Spotify.").
        rest = [p for p in re.split(r"(?<=[.!?;])\s+", _DIDNT.sub("", text)) if p.strip(" ,.!?;")]
        calls = []
        for c, is_clause in [(c, True) for c in clauses] + [(r, False) for r in rest]:
            c = _COMPLAINT_FILLER.sub("", c)
            c = re.sub(r"\b(\w+)\b", lambda m: _BASE.get(m.group(1).lower(), m.group(1)), c, count=1)
            c = re.sub(r"\s+", " ", c).strip(" ,.!?;")
            if not c:
                continue
            got = self._compound(c, ctx) or ([{"tool": i[0], "args": i[1]}] if (i := match_intent(c)) else None)
            if not got:
                if is_clause or _ACTION_REQUEST.search(c):
                    return None                  # something we don't understand: the model (or Claude) decides
                continue                          # "dude", "first of all": nothing to do
            calls += got
        return calls[:4] or None

    def _fast_path(self, text: str, ctx: ToolContext) -> bool:
        grid = ctx.services.get("grid")
        return bool(match_routine(text, self.settings) or match_intent(
            text, grid_visible=bool(grid and grid.grid is not None), labels=bool(grid and grid.labels is not None),
            browser_active=_browser_active()))

    async def _say(self, conv: Conversation, user_text: str, reply: str) -> AsyncIterator[Event]:
        conv.messages.append({"role": "user", "content": user_text})
        conv.messages.append({"role": "assistant", "content": reply})
        conv.trim()
        yield TextDelta(reply)
        yield TurnComplete(reply, {}, {"input_tokens": 0, "output_tokens": 0}, "learned")

    async def _note_preference(self, pref: str, ctx: ToolContext, sir: str) -> str:
        from assistant.core.memory import SecretRefused, get_store
        store = ctx.services.get("memory") or get_store(self.settings)
        if store is None:
            return f"Noted{sir}."
        try:
            await asyncio.to_thread(store.add, pref)
        except SecretRefused:
            return "I won't keep that: it looks like a password or card number."
        except ValueError:
            return f"Noted{sir}."
        return f"Noted{sir}. I'll remember that."

    async def _replay(self, conv: Conversation, user_text: str, lesson, ctx: ToolContext) -> AsyncIterator[Event]:
        """Do what the user taught for this request, the way that fixed it last time."""
        async for ev in self._run_calls(conv, user_text, lesson.calls, ctx):
            yield ev
        conv.last_action = {"text": user_text, "at": time.time(), "lesson": lesson.id}

    async def _run_calls(self, conv: Conversation, user_text: str, calls: list[dict], ctx: ToolContext) -> AsyncIterator[Event]:
        """Run known tool calls directly (no model) and say their results."""
        t0 = time.perf_counter()
        said: list[str] = []
        for i, call in enumerate(calls):
            cid = f"learned_{i}"
            yield ToolStarted(cid, call["tool"], dict(call.get("args") or {}))
            started = time.perf_counter()
            res = await self.registry.execute(call["tool"], dict(call.get("args") or {}), ctx)
            text = res.content if isinstance(res.content, str) else _summarize_content(res.content, 300)
            yield ToolFinished(cid, call["tool"], res.is_error, _summarize_content(text, 200), _ms(started))
            said.append(text.strip())
            if res.is_error:
                break
        spoken = " ".join(s for s in said if s) or "Done."
        conv.messages.append({"role": "user", "content": user_text})
        conv.messages.append({"role": "assistant", "content": spoken})
        conv.trim()
        yield TextDelta(spoken)
        yield TurnComplete(spoken, {"total_ms": _ms(t0)}, {"input_tokens": 0, "output_tokens": 0}, "learned")

    # --- main loop ----------------------------------------------------------------
    async def _turn(self, conv: Conversation, user_text: str, ctx: ToolContext,
                    extra_context: dict[str, str] | None = None) -> AsyncIterator[Event]:
        ctx.services.setdefault("brain", self)
        t0 = time.perf_counter()
        timings: dict[str, float] = {}
        usage = {"input_tokens": 0, "output_tokens": 0}
        spoken: list[str] = []
        checkpoint = conv.checkpoint()
        from assistant.brain.situation import situation
        now = await situation()                          # which window is in front, the browser, media
        up = self._upcoming(ctx)
        if up:
            now["timers and reminders set"] = up
        conv.messages.append({"role": "user", "content": turn_context(
            self.settings, {**now, **(extra_context or {})}, self._memories(user_text, ctx)) + "\n" + user_text})
        rounds = 0
        tools_used = False
        nudged = False
        # Common commands ("pause", "what's playing", "play X") skip the model entirely.
        grid = ctx.services.get("grid")
        from assistant.brain.intents import _clean
        from assistant.tools.teach import teach_intent
        teacher = ctx.services.get("teacher")
        lesson = teach_intent(_clean(user_text), teacher) if teacher and (
            teacher.recording or teacher.awaiting_name) else None
        intent = (lesson or match_routine(user_text, self.settings)
                  or match_intent(user_text, browser_active=_browser_active(),
                                  grid_visible=bool(grid and grid.grid is not None),
                                  labels=bool(grid and grid.labels is not None))
                  if self.cfg.fast_commands else None)
        if intent is not None:
            async for ev in self._fast_command(conv, intent, ctx, t0, timings):
                yield ev
            return
        # "Ask Claude ..." goes straight to the expert; no chance for the small model to skip it.
        forced = direct_escalation(user_text, conv)
        try:
            while True:
                allow_tools = rounds < self.settings.brain.max_tool_rounds
                if forced is not None:
                    calls, forced = [forced], None
                    conv.messages.append({"role": "assistant", "content": "", "tool_calls": calls})
                    text_parts = None  # already recorded in history
                else:
                    out = _Round()
                    # The retry after a nudge is held back until we know whether it acted:
                    # otherwise a model that ignores the nudge says the same thing twice.
                    hold = nudged and not tools_used
                    held: list[TextDelta] = []
                    first = True             # only a round's first text may need a space before it
                    # "Sure, I'll open that for you." then a tool call then "Spotify is open." is
                    # twice the talking. A round that starts like a preamble is held back and
                    # dropped if it calls a tool (the result gets confirmed instead).
                    lead: list[TextDelta] | None = [] if allow_tools and not hold else None
                    # Asked to *do* something: say nothing until it's clear whether a tool was
                    # used, so a false "done" is never spoken (it gets nudged instead).
                    doing = not tools_used and _is_action_request(user_text)
                    try:
                        async for ev in self._model_round(conv, allow_tools, out, timings, usage, t0):
                            if hold:
                                held.append(ev)
                                continue
                            if lead is not None:
                                lead.append(ev)
                                so_far = "".join(e.text for e in lead).lstrip()
                                if len(so_far.split()) < 3 and not re.search(r"[.!?,]", so_far):
                                    continue                 # too early to tell
                                if doing or _PREAMBLE.match(so_far):
                                    continue                 # keep holding until the round ends
                                pending, lead = lead, None
                                for e in pending:
                                    if first:
                                        e, first = _spaced(e, spoken), False
                                    spoken.append(e.text)
                                    yield e
                                continue
                            if first:
                                ev, first = _spaced(ev, spoken), False
                            spoken.append(ev.text)
                            yield ev
                    except _RetryWithoutThink:
                        continue
                    text_parts, calls = out.text, out.calls
                    claimed = lead and not calls and not nudged and _acts_without_tools(
                        "".join(e.text for e in lead), user_text)
                    if lead and not calls and not claimed:   # held text, but no tool: it was the answer
                        for e in lead:
                            if first:
                                e, first = _spaced(e, spoken), False
                            spoken.append(e.text)
                            yield e
                    if hold:
                        if not calls and _acts_without_tools("".join(text_parts), user_text):
                            who = self.settings.assistant.address_user_as
                            text_parts = [f"Sorry{', ' + who if who else ''}, {_STUCK}"]
                            held = [TextDelta(text_parts[0])]
                        for i, ev in enumerate(held):
                            ev = _spaced(ev, spoken) if i == 0 else ev
                            spoken.append(ev.text)
                            yield ev

                if text_parts is not None:
                    assistant_msg: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
                    if calls:
                        assistant_msg["tool_calls"] = calls
                    conv.messages.append(assistant_msg)
                if not calls:
                    # Small models sometimes *say* they'll do something (or that it's done)
                    # without calling a tool. Nudge once.
                    said_text = "".join(text_parts or [])
                    if (not tools_used and not nudged and allow_tools
                            and _acts_without_tools(said_text, user_text)):
                        nudged = True
                        conv.messages.append({"role": "user", "nudge": True, "content":
                                              NUDGE_CAN if _refuses(said_text, user_text) else
                                              NUDGE_CLAIM if _CLAIM.search(said_text) else NUDGE})
                        continue
                    break
                tools_used = True

                rounds += 1
                named = [(f"local_{rounds}_{i}", c.get("function", {})) for i, c in enumerate(calls)]
                for cid, fn in named:
                    args = fn.get("arguments")
                    if isinstance(args, str):   # some models return a JSON string
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            pass
                    fn["arguments"] = args
                    yield ToolStarted(cid, fn.get("name", "?"), args if isinstance(args, dict) else {})
                started = time.perf_counter()
                async def run(fn: dict) -> ToolResult:
                    wrong = _wrong_target(fn.get("name", ""), fn.get("arguments"), user_text)
                    if wrong:
                        return ToolResult(wrong, is_error=True)
                    return await self.registry.execute(fn.get("name", ""), fn.get("arguments"), ctx)
                results = await asyncio.gather(*(run(fn) for _, fn in named))
                timings["tools_ms"] = timings.get("tools_ms", 0) + _ms(started)
                for (cid, fn), res in zip(named, results):
                    text = await self._result_text(res, fn.get("arguments"))
                    conv.messages.append({"role": "tool", "tool_name": fn.get("name", ""),
                                          "content": text})
                    yield ToolFinished(cid, fn.get("name", "?"), res.is_error,
                                       _summarize_content(text, 200), _ms(started))

        except asyncio.CancelledError:
            conv.rollback(checkpoint)
            said = "".join(spoken).strip()
            conv.messages.append({"role": "user", "content": user_text})
            conv.messages.append({"role": "assistant",
                                  "content": (said + " [interrupted]") if said else "[interrupted]"})
            raise
        except httpx.ConnectError:
            conv.rollback(checkpoint)
            yield BrainError("I can't reach my local brain. Is the Ollama app running?")
            return
        except (httpx.TimeoutException, OllamaError) as e:
            conv.rollback(checkpoint)
            yield BrainError(str(e) or "My local brain timed out.")
            return

        conv.trim()
        timings["total_ms"] = _ms(t0)
        yield TurnComplete("".join(spoken).strip(), timings, usage, "end_turn")

    @staticmethod
    def _move_shortcuts(store) -> None:
        """Memories that are really shortcuts ("when I tell you to open my business, open
        business.facebook.com") become taught shortcuts: as a fact the model read them out and
        claimed to be acting on them (owner's case)."""
        from assistant.brain import lessons as L
        moved = []
        for m in store.all():
            taught = L.teaching(m.text)
            if taught and L.get_lessons().learn_meaning(taught[0], taught[1], m.text):
                moved.append(m.id)
        if moved:
            store.delete(moved)
            log.info("moved %d shortcut(s) from memory to taught shortcuts", len(moved))

    def _memories(self, user_text: str, ctx: ToolContext) -> list[str]:
        """Remembered facts relevant to this request (all of them while there are few)."""
        from assistant.core.memory import get_store
        try:
            store = ctx.services.get("memory") or get_store(self.settings)
            if store:
                self._move_shortcuts(store)
            return store.for_turn(user_text, self.settings.memory.per_turn) if store else []
        except Exception:
            log.exception("memory lookup failed")
            return []

    async def _fast_command(self, conv: Conversation, intent: tuple[str, dict], ctx: ToolContext,
                            t0: float, timings: dict) -> AsyncIterator[Event]:
        name, args = intent
        cid = "fast_1"
        yield ToolStarted(cid, name, args)
        started = time.perf_counter()
        res = await self.registry.execute(name, args, ctx)
        timings["tools_ms"] = _ms(started)
        # A screenshot is described first (the fast path speaks the result, it can't show a picture).
        text = res.content if isinstance(res.content, str) else \
            (await self._result_text(res, args)).replace("Screen description: ", "")
        text = "\n".join(line for line in text.splitlines() if not line.startswith("Screenshot of monitor"))
        yield ToolFinished(cid, name, res.is_error, _summarize_content(text, 200), _ms(started))
        spoken = text.strip() or "Done."
        timings["first_token_ms"] = _ms(t0)
        yield TextDelta(spoken)
        # Record it in the conversation as plain text, so the model knows what happened.
        conv.messages.append({"role": "assistant", "content": spoken})
        conv.trim()
        timings["total_ms"] = _ms(t0)
        yield TurnComplete(spoken, timings, {"input_tokens": 0, "output_tokens": 0}, "fast_command")

    def _upcoming(self, ctx: ToolContext) -> str:
        """The timers and reminders that are set, for the model's context. Owner's case: the phone
        showed "junk job at 10am" under Up next, and Nova said it saw no such reminder."""
        sched = ctx.services.get("scheduler")
        if sched is None:
            return ""
        try:
            items = sched.upcoming()[:5]
        except Exception:
            return ""
        tz = ZoneInfo(self.settings.assistant.timezone)
        out = []
        for r in items:
            left = max(0, int(r.due - time.time()))
            h, m = divmod(left // 60, 60)
            when = datetime.fromtimestamp(r.due, tz).strftime("%H:%M")
            out.append(f"{r.kind} '{r.text or r.kind}' at {when} (in {f'{h} h ' if h else ''}{m} min)")
        return "; ".join(out)

    async def _model_round(self, conv: Conversation, allow_tools: bool, out: "_Round",
                           timings: dict, usage: dict, t0: float) -> AsyncIterator[TextDelta]:
        """One streamed model response: yields speakable text, collects tool calls in `out`."""
        think = ThinkFilter(hold=self._hold_think)
        notes = ContextFilter()
        async for chunk in self._stream(self._body(self._fitted(conv.messages, allow_tools), tools=allow_tools)):
            msg = chunk.get("message") or {}
            # msg["thinking"] (separated reasoning) is never spoken, nor a copied <context> block.
            text = notes.feed(think.feed(msg.get("content") or ""))
            if chunk.get("done"):
                text += notes.feed(think.flush()) + notes.flush()
            if text:
                timings.setdefault("first_token_ms", _ms(t0))
                out.text.append(text)
                yield TextDelta(text)
            out.calls.extend(msg.get("tool_calls") or [])
            if chunk.get("done"):
                usage["input_tokens"] += chunk.get("prompt_eval_count", 0)
                usage["output_tokens"] += chunk.get("eval_count", 0)

    async def _result_text(self, res: ToolResult, args: Any) -> str:
        """Ollama tool messages are text-only; describe images (screenshots) first."""
        if isinstance(res.content, str):
            return ("ERROR: " if res.is_error else "") + res.content
        parts = []
        focus = args.get("focus", "") if isinstance(args, dict) else ""
        for block in res.content:
            if block.get("type") == "text":
                parts.append(block["text"])
            elif block.get("type") == "image":
                try:
                    parts.append("Screen description: " + await self.describe_image(
                        block["source"]["data"], focus))
                except (OllamaError, ExpertError, httpx.HTTPError) as e:
                    parts.append(f"ERROR: couldn't analyse the screenshot ({e})")
        return "\n".join(parts)

    async def describe_image(self, b64_jpeg: str, focus: str = "") -> str:
        question = ("Describe what is on this screen for a voice assistant. Read out any error "
                    "messages or dialog text exactly. Be concise." +
                    (f" Focus on: {focus}." if focus else ""))
        if self.cfg.vision == "claude_code":
            with tempfile.TemporaryDirectory(dir=_workspace(self.expert)) as d:
                path = Path(d) / "screen.jpg"
                path.write_bytes(base64.b64decode(b64_jpeg))
                return await self.expert.ask(question, image_path=path)
        b64_jpeg = shrink_jpeg(b64_jpeg, self.cfg.vision_max_px)
        body = {"model": self.cfg.vision_model, "stream": True, "think": False,
                "keep_alive": self.cfg.vision_keep_alive,
                "messages": [{"role": "user", "content": question, "images": [b64_jpeg]}]}
        out = []
        stats: dict = {}
        for attempt in range(2):
            try:
                async for chunk in self._stream(body):
                    out.append((chunk.get("message") or {}).get("content", ""))
                    if chunk.get("done"):
                        stats = chunk
                break
            except _RetryWithoutThink:
                body.pop("think", None)
        self.last_vision_stats = {k: round(stats.get(k, 0) / 1e6) for k in
                                  ("load_duration", "prompt_eval_duration", "eval_duration")}
        log.info("vision %s: load %d ms, image+prompt %d ms, answer %d ms", self.cfg.vision_model,
                 *self.last_vision_stats.values())
        text = "".join(out)
        if ThinkFilter.CLOSE in text:        # drop any reasoning that leaked into the answer
            text = text.split(ThinkFilter.CLOSE, 1)[1]
        return text.replace(ThinkFilter.OPEN, "").strip()

    async def ask_expert(self, task: str, context: str = "", image_path=None) -> str:
        return await self.expert.ask(task, context, image_path)


class _RetryWithoutThink(Exception):
    pass


def _spaced(ev: TextDelta, spoken: list[str]) -> TextDelta:
    """A new round's text after earlier text needs a space ("shortly.I will" -> "shortly. I will")."""
    if spoken and ev.text and not ev.text[0].isspace() and not spoken[-1][-1:].isspace():
        return TextDelta(" " + ev.text)
    return ev


class _Round:
    def __init__(self) -> None:
        self.text: list[str] = []
        self.calls: list[dict] = []


_SMALL_TALK = [
    (r"(?:thanks|thank you|cheers|ta|thanks a lot|thank you so much|appreciate it|nice one|good job|well done|perfect)",
     "You're welcome{sir}."),
    (r"(?:hello|hi|hey|hiya|yo|good (?:morning|afternoon|evening))(?: there)?", "{greet}{sir}."),
    (r"(?:how are you|how are you doing|how'?s it going|you good|you alright)", "Very well, thank you{sir}. And you?"),
    (r"(?:goodnight|good night|night)", "Good night{sir}."),
    (r"(?:who (?:made|created|built|programmed|coded) you|who(?:'?s| is) your (?:creator|maker))",
     "{owner} did{sir}. I work for {owner}."),
    (r"(?:who(?:'?s| is) your (?:master|boss|owner)|who do you (?:work for|serve|belong to))", "{owner}{sir}."),
    (r"(?:who are you|what(?:'?s| is) your name|what are you|what do i call you)",
     "I'm {name}{sir}, your assistant. {owner} made me."),
    (r"(?:what can you do|what are you able to do|what can i ask you|what do you do|help|what are your (?:skills|abilities))",
     "Quite a lot{sir}. Open, switch and close apps; play music and control videos; timers, reminders and "
     "alarms; the weather and sums; search the web or drive my own browser; press keys and click things "
     "for you; and remember what you tell me. Just ask."),
]


def _small_talk(text: str, sir: str, owner: str = "", name: str = "Nova") -> str | None:
    """Greetings and thanks: an instant, fixed reply instead of waiting for the model."""
    t = re.sub(r"[^\w\s']", "", text.lower()).strip()
    t = re.sub(r"\b(?:nova|sir|mate|buddy)\b", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t and re.search(r"\bnova\b", text, re.I):
        return f"Yes{sir}?"                      # just the name (it used to repeat the last reply)
    for pat, reply in _SMALL_TALK:
        if re.fullmatch(pat, t):
            h = time.localtime().tm_hour
            said = re.search(r"good (morning|afternoon|evening)", t)
            greet = (f"Good {said.group(1)}" if said else
                     "Good morning" if h < 12 else "Good afternoon" if h < 18 else "Good evening")
            return reply.format(sir=sir, greet=greet, owner=owner or "You", name=name)
    return None


_DIDNT = re.compile(r"\byou (?:just\s+|still\s+|also\s+)?(?:didn'?t|did not|never|haven'?t|have not|still haven'?t|"
                    r"still didn'?t|forgot to)\s+(.+?)(?=\s*[.!?;]|\s*,?\s*\b(?:and|but)\s+(?:you|also)\b|$)", re.I)
# "you still haven't closed Spotify" -> "close spotify" (the fast path knows the plain verb).
_BASE = {"closed": "close", "opened": "open", "reopened": "reopen", "skipped": "skip", "paused": "pause",
         "played": "play", "unpaused": "unpause", "resumed": "resume", "muted": "mute", "unmuted": "unmute",
         "took": "take", "taken": "take", "switched": "switch", "stopped": "stop", "started": "start",
         "launched": "launch", "turned": "turn", "typed": "type", "pressed": "press", "clicked": "click",
         "went": "go", "gone": "go", "moved": "move", "minimized": "minimize", "minimised": "minimise",
         "maximized": "maximize", "maximised": "maximise", "brought": "bring", "quit": "quit", "put": "put"}
_COMPLAINT_FILLER = re.compile(r"\b(?:yet|either|at all|properly|like i asked|when i asked|for me|first of all|again|"
                               r"like i said|dude|bro|man|mate|anything|any of it|a thing)\b", re.I)
_SPLIT = re.compile(r"\s*,?\s*\b(?:and then|and also|and|then|also)\b\s*|\s*,\s*|(?<=[?.!])\s+", re.I)

_OPEN_BROWSER = re.compile(
    r"^(?:(?:but|so|then|ok|okay|can you|could you|why don'?t you|just|please|go ahead and|nova)[, ]+)*"
    r"(?:open|use|go (?:on|onto|to)|check|pull up|launch|bring up)(?: up)? (?:microsoft )?(?:edge|the browser|your browser|"
    r"a browser|the internet|the web|online)(?:,? (?:and|to) (?:look|check|search|find|see)(?: (?:it|that) up| for it| there|"
    r" it| online)?)?(?: for me| please)?[.!?]*$", re.I)


def _open_browser_followup(text: str, last_query: str | None) -> dict | None:
    """'Why don't you open Edge and look' -> search what was just asked about, in Nova's browser.
    (Owner's case: the model answered "I cannot open Edge or any browser".)"""
    if not _OPEN_BROWSER.match(text.strip()):
        return None
    if last_query:
        return {"tool": "browser", "args": {"action": "search", "text": last_query, "details": False}}
    return {"tool": "browser", "args": {"action": "open", "site": "duckduckgo.com", "details": False}}


def _browser_active() -> bool:
    from assistant.tools.browser import BROWSER
    return BROWSER.active


_PROHIBIT = re.compile(r"^\W*(?:no+\b[\s,.!]*|nova\b[\s,.!]*)*(?:please\s+)?(?:stop|don'?t|do not|never(?! ?mind)|quit|cut it out|"
                       r"enough|you keep|why do you keep|why are you)\b", re.I)
_ABOUT_SITE = re.compile(r"\b(?:website|site|page|url|link|there|that place)\b|\b(?:take|taking|send|sending|open|opening|go|going|"
                         r"load|loading|bring|bringing)\b.*\b(?:that|it|there)\b", re.I)


def _prohibition(text: str) -> str | None:
    """What to say to 'stop taking me to that website' / 'don't do that again' (no action), or
    None when it's a normal request ('stop the music' is a command)."""
    if not _PROHIBIT.match(text):
        return None
    from assistant.brain.intents import match_intent
    try:
        if match_intent(text) is not None:
            return None
    except Exception:
        pass
    if _ABOUT_SITE.search(text):
        from assistant.tools import sitecheck
        host = sitecheck.LAST_OPENED.get("host")
        if host:
            sitecheck.block(host)
            return (f"Understood{{sir}}. I won't open {host} again; it's blocked. "
                    f"Say \"unblock {host}\" if you ever want it back.")
    return "Understood{sir}. I won't."


_STUCK = "I wasn't able to do that."
# PC actions worth working out when they fail (not deliberate refusals like a banned site).
_RESCUABLE = {"click_element", "app", "my_browser", "video", "window", "open_app", "press_keys", "type_text",
              "open_website", "media", "play_music", "music_control", "mouse", "open_file"}
_DELIBERATE = ("You told me never", "The user declined", "I don't type into a command window", "Which ",
               "Where to?", "Click what?", "Type what?", "Find what?")
_CANT = re.compile(r"\b(?:i|nova) (?:can ?not|can'?t|couldn'?t|could not|am unable|'?m unable|am not able|'?m not able|"
                   r"wasn'?t able|was not able|don'?t have (?:the )?(?:ability|access|a way)|do not have (?:the )?(?:ability|access))\b"
                   r"|\bnot a valid action\b|\bisn'?t (?:possible|something i can)\b"
                   # the ways a small model says no without "I can't" (owner: "if I tell it to do
                   # something it does it, not 'oh sir I can't do that'")
                   r"|\b(?:not|isn'?t|is not) something i(?:'m| am)? (?:able|allowed|permitted|designed|equipped)\b"
                   r"|\bbeyond (?:what i can|my (?:abilities|capabilities|reach|control))\b|\boutside (?:of )?my (?:abilities|capabilities)\b"
                   r"|\bi (?:don'?t|do not) have (?:the )?(?:capability|permission|tools?|means|option)\b"
                   r"|\b(?:there'?s|there is) no way (?:for me )?to\b|\bi(?:'m| am) (?:not|unable) (?:allowed|permitted)\b"
                   r"|\bi(?:'m| am) afraid (?:i|that|that's|this)\b|\bunfortunately,? (?:i|that|this|it)\b", re.I)
# "Done" said about the PC: only true if a tool actually did it this turn.
_LEAKED_CALL = re.compile(r"^\s*(?:>\s*[a-z_]+(?:\s+[a-z_]+){0,3}\s*(?:\n+|$))+", re.I)
_SAID_THIS = re.compile(r"\b(?:this|that|it|the window|this window|that window|the app|this app|current)\b", re.I)


def _wrong_target(tool: str, args, request: str) -> str | None:
    """The small model closing 'this' (whatever is in front) when the user named an app: "just go
    ahead and close Spotify" closed Firefox (owner's case). Refused back to the model."""
    if tool != "window" or not isinstance(args, dict):
        return None
    from assistant.tools.pc import THIS
    if args.get("action") in ("close", "quit", "minimize") and str(args.get("app", "")).lower().strip() in THIS \
            and not _SAID_THIS.search(request):
        return ("Not done: the user didn't say 'this'; 'this' would close whatever window is in front. "
                "Call window again with app set to the app the user named.")
    return None


_DONE_CLAIM = re.compile(      # "Skipped the song.", "I've closed Spotify.": a claim, whatever was asked
    r"(?:^|[.!?]\s+)(?:(?:ok|okay|done|right|certainly|sure|alright)[,.!]?\s+)?(?:sir[,.]?\s+)?(?:i(?:'ve| have)?\s+)?"
    r"(?:just\s+|now\s+|also\s+)?(?:re)?(?:opened|closed|skipped|paused|played|resumed|unpaused|switched|launched|"
    r"started|stopped|muted|unmuted|minimi[sz]ed|maximi[sz]ed|full[- ]?screened|moved|typed|pressed|clicked|"
    r"searched|navigated|brought|quit|killed|loaded|went|took)\b"
    r"|\b(?:took|taken) you\b|\bskipped to\b", re.I)
# "I'm sorry, but I can't help with that": the small model being over-careful with the owner's own
# request (owner: "it says I can't help you with that ... I want to be able to do anything with Nova").
_HELP_REFUSAL = re.compile(
    r"\bi (?:can'?t|cannot|can not|won'?t|will not|am not able to|'m not able to|am unable to|'m unable to) "
    r"(?:help|assist|provide|comply|fulfil+|answer|discuss|talk about|write|create|generate|engage|support|"
    r"give you|share|go into|do that for you)\b"
    r"|\bi(?:'m| am) not (?:comfortable|able to help|going to (?:help|answer|write|discuss))\b"
    r"|\bagainst my (?:guidelines|policies|policy|programming|principles)\b|\bi must (?:decline|refuse)\b"
    r"|\b(?:that'?s|that is|it'?s|it is) not (?:appropriate|something i can (?:help|assist|discuss))\b"
    r"|\bi(?:'d| would) prefer not to\b|\bas an ai\b", re.I)
_DOING_CLAIM = re.compile(     # "Opening business.facebook.com in your browser, sir.": said as it happens
    r"^(?:(?:ok|okay|sure|certainly|right|alright|all right|very well|of course)[,.!]?\s+)?(?:sir[,.]?\s+)?"
    r"(?:i'?m\s+|i am\s+)?(?:now\s+)?(?:re)?(?:opening|closing|launching|skipping|pausing|resuming|switching|"
    r"typing|clicking|navigating|taking you|bringing up|pulling up|loading|muting|unmuting|minimi[sz]ing|"
    r"maximi[sz]ing|quitting|stopping|playing|putting on|turning (?:up|down|on|off))\b", re.I)
_STATE_CLAIM = re.compile(     # "Firefox is reopened", "Now on YouTube": only a claim when an action was asked
    r"\b(?:is|are|has been|have been)\s+(?:now\s+)?(?:re)?(?:open(?:ed)?|closed|loaded|playing|paused|skipped|"
    r"muted|minimi[sz]ed|maximi[sz]ed|stopped|launched)\b"
    r"|(?:^|[.!?]\s+)(?:you(?:'re| are)\s+)?(?:now|back) on \w", re.I)
_ABOUT_NOVA = re.compile(r"\byou (?:just |still |also )?(?:didn'?t|did not|haven'?t|have not|never|forgot|closed|opened|"
                         r"broke|messed|killed|skipped|paused|stopped|lost)\b", re.I)
_MORE_ACTIONS = re.compile(r"\b(?:rearrange|sort|organi[sz]e|sign|log|download|install|join|message|reply|post|scroll|"
                           r"select|drag|copy|paste|rename|fill|book|order|subscribe|follow|like|share|upload)\b", re.I)
_WORK_IT_OUT = re.compile(
    r"^(?:nova[, ]+)?(?:(?:just|please|can you|could you|then|ok|okay|so)[, ]+)*(?:figure (?:it|that|this) out|"
    r"work (?:it|that|this) out|do it yourself|take (?:over|control)|work through it|find a way|"
    r"try (?:harder|again|another way|a different way)|use your (?:brain|head)|think about it)\b", re.I)


_NEVER_MIND = re.compile(r"^\W*(?:never ?mind|forget it|nothing|cancel|it'?s fine|no|nah|don'?t worry)\b", re.I)
_CORRECTION = re.compile(r"^\s*(?:no[,.!]?\s+|nope[,.!]?\s+|sorry[,.!]?\s+|actually[,.!]?\s+)*"
                         r"(?:i meant|i said|i mean)\s+(.+?)\s*$", re.I)

# How a round starts when the model is about to act: held back in case a tool call follows.
_PREAMBLE = re.compile(
    r"(?:(?:sure|certainly|of course|absolutely|okay|ok|alright|all right|right|very well|no problem|"
    r"yes|got it|understood)\b[\s,.!-]*(?:sir\b[\s,.!-]*)?)*"
    r"(?:i'?ll|i will|let me|i'?m going to|i am going to|one moment|give me a (?:moment|second)|"
    r"right away|on it|opening|launching|starting|setting|turning|playing|pausing|checking|looking|"
    r"switching|closing|minimi[sz]ing|maximi[sz]ing|searching|getting|creating|adding|sending|"
    r"(?:sure|certainly|of course|absolutely|okay|ok|alright|all right|very well|no problem)\b)", re.I)

_PROMISE = re.compile(
    r"\b(i'?ll|i will|let me|on it|one moment|give me a moment|checking|i'?m going to|"
    r"right away|looking (?:into|at)|i'?ll (?:check|look|ask|find|open))\b", re.I)

# "... is done", "the video is now fullscreen", "I've minimized it": claims that only a tool
# could make true (owner's case: three actions reported done, none performed).
_CLAIM = re.compile(
    r"\b(?:is|are|has been|have been)\s+(?:now\s+)?(?:open(?:ed)?|closed|minimi[sz]ed|maximi[sz]ed|"
    r"paused|unpaused|resumed|(?:in\s+)?full\s?screen|muted|unmuted|locked|started|stopped|"
    r"cancel(?:l)?ed|turned (?:on|off)|switched (?:on|off)|up|down)\b"
    r"|\bi(?:'?ve| have)\s+(?:now\s+|just\s+|also\s+)?(?:opened|closed|minimi[sz]ed|maximi[sz]ed|paused|unpaused|"
    r"resumed|started|set|turned|switched|muted|locked|played|cancel(?:l)?ed|done)\b"
    r"|\b\w+ing\b[^.]{0,80}\b(?:is|are) (?:now )?done\b"
    # Owner: "he keeps saying I played and full screened the video but isn't doing anything".
    r"|\bi\s+(?:just\s+|also\s+)?(?:opened|closed|paused|unpaused|played|resumed|started|put|made|turned|"
    r"switched|launched|skipped|full[- ]?screened|maximi[sz]ed|minimi[sz]ed|muted|locked)\b"
    r"|\bi(?:'?ve| have)\s+(?:now\s+|just\s+|also\s+)?(?:launched|skipped|put|made|full[- ]?screened)\b"
    r"|\b(?:is|are)\s+(?:now\s+)?playing\b|(?:^|[.!?]\s+)(?:now\s+)?playing\s+\w", re.I)

# Owner's case: "I cannot open Edge or any browser directly as part of this interaction" and
# "I cannot directly navigate to kbb.com". It can: it has a browser tool and controls the PC.
_REFUSES = re.compile(
    r"\bi (?:can ?not|can'?t|am unable to|'?m unable to|am not able to|'?m not able to|don'?t have the ability to|"
    r"do not have the ability to|don'?t have access to|do not have access to|have no way to)\s+(?:directly\s+)?"
    r"(?:open|browse|navigate|access|visit|go to|control|use|search|click|launch|look at|see|check|play|type|"
    r"interact with)\b|\bas an ai\b|\bas part of this interaction\b", re.I)
NUDGE_CAN = ("(Note from the system, not the user: you CAN do this. You have a browser tool that opens and "
             "controls websites (open, search, click, read), and tools that control this PC. Call the right tool "
             "now instead of saying you can't.)")

NUDGE = ("(Note from the system, not the user: you said you would do that but did not call a "
         "tool. Call the right tool now. If no tool can do it, say so in one short sentence.)")
NUDGE_CLAIM = ("(Note from the system, not the user: you said it was done, but you did not call any "
               "tool, so nothing happened. Call the right tools now; you can call several at once. "
               "If no tool can do part of it, say which part in one short sentence.)")


_ACTION_REQUEST = re.compile(
    r"\b(?:open|close|minimi[sz]e|maximi[sz]e|pause|unpause|resume|play|turn|set|mute|unmute|lock|"
    r"full\s?screen|start|stop|switch|launch|cancel|volume|skip|show|hide|search|remind|put|make|go|"
    r"fire|get|pull|bring|load|run|kill|shut|change|give|take|send|type|click|press|find|move|delete|"
    r"create|write|save|add|remove|raise|lower|increase|decrease|crank|boost|max|minimi[sz]e|restore|"
    r"quit|exit|reopen|refresh|reload|record|clip|watch|scroll|zoom|email|text|message|call|buy|order|"
    r"download|install|uninstall|update|empty|clear|sort|organi[sz]e|rename|edit|fix|join|leave|connect|"
    r"disconnect|enable|disable|toggle|select|highlight|copy|paste|undo|redo|print|share|upload|screenshot|"
    r"capture|translate|dim|brighten|sleep|restart|reboot|log ?in|log ?out|sign ?in|sign ?out|reply|accept|"
    r"decline|mark|read|try|use)\b", re.I)
_QUESTION = re.compile(r"^\W*(?:is|are|was|were|does|do|did|when|what|where|why|how|which|who)\b", re.I)


def _is_action_request(request: str) -> bool:
    return bool(_ACTION_REQUEST.search(request)) and not _QUESTION.match(request)


def _refuses(text: str, request: str | None = None) -> bool:
    """Said it can't. For an instruction, any "I can't" / "I'm afraid" / "unfortunately" counts:
    it has tools for the whole PC and gets nudged to use one (owner: "if I tell it to do something
    it does it")."""
    if _HELP_REFUSAL.search(text):
        return False                   # "can't help with that" is answered by Claude (_answer_instead)
    return bool(_REFUSES.search(text) or (request and _is_action_request(request) and _CANT.search(text)))


def _acts_without_tools(text: str, request: str | None = None) -> bool:
    """Promised an action, claimed one was done, or wrongly said it can't (it has the tools).
    A claim only counts when the user asked for an action ("is the shop open?" -> "it's closed
    on Sundays" is just an answer)."""
    if _PROMISE.search(text):
        return True
    if _refuses(text, request):
        return True
    if not _CLAIM.search(text):
        return False
    return request is None or (bool(_ACTION_REQUEST.search(request)) and not _QUESTION.match(request))

_ASK_CLAUDE = re.compile(
    r"^\s*(?:please\s+)?(?:can you\s+|could you\s+)?(?:ask|have|get|tell)\s+claude\s*"
    r"(?:to\s+|about\s+|,|:)?\s*(?P<task>.+)$", re.I | re.S)


def direct_escalation(user_text: str, conv: Conversation) -> dict | None:
    """'Ask Claude ...' -> an escalate call, with recent conversation as context."""
    m = _ASK_CLAUDE.match(user_text)
    if not m or len(m.group("task").strip()) < 3:
        return None
    recent = []
    for msg in conv.messages[-7:-1]:  # skip the message we just added
        if msg["role"] in ("user", "assistant") and isinstance(msg.get("content"), str) and msg["content"]:
            text = msg["content"].split("</context>")[-1].strip()
            recent.append(f"{msg['role']}: {text}")
    args = {"task": m.group("task").strip()}
    if recent:
        args["context"] = "Recent conversation with the user:\n" + "\n".join(recent)
    return {"function": {"name": "escalate", "arguments": args}}


def shrink_jpeg(b64_jpeg: str, max_px: int) -> str:
    """Downscale a base64 JPEG; small vision models are far faster on smaller images."""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(base64.b64decode(b64_jpeg)))
    if max(img.size) <= max_px:
        return b64_jpeg
    img.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _workspace(expert: Expert) -> str | None:
    ws = getattr(expert, "workspace", None)
    if ws is not None:
        Path(ws).mkdir(parents=True, exist_ok=True)
        return str(ws)
    return None
