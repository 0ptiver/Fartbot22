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
from pathlib import Path
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

    # --- main loop ----------------------------------------------------------------
    async def run_turn(self, conv: Conversation, user_text: str, ctx: ToolContext,
                       extra_context: dict[str, str] | None = None) -> AsyncIterator[Event]:
        ctx.services.setdefault("brain", self)
        t0 = time.perf_counter()
        timings: dict[str, float] = {}
        usage = {"input_tokens": 0, "output_tokens": 0}
        spoken: list[str] = []
        checkpoint = conv.checkpoint()
        conv.messages.append({"role": "user",
                              "content": turn_context(self.settings, extra_context) + "\n" + user_text})
        rounds = 0
        tools_used = False
        nudged = False
        # Common commands ("pause", "what's playing", "play X") skip the model entirely.
        grid = ctx.services.get("grid")
        intent = (match_routine(user_text, self.settings)
                  or match_intent(user_text, grid_visible=bool(grid and grid.visible))
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
                    try:
                        async for ev in self._model_round(conv, allow_tools, out, timings, usage, t0):
                            if hold:
                                held.append(ev)
                                continue
                            if first:
                                ev, first = _spaced(ev, spoken), False
                            spoken.append(ev.text)
                            yield ev
                    except _RetryWithoutThink:
                        continue
                    text_parts, calls = out.text, out.calls
                    if hold:
                        if not calls and _acts_without_tools("".join(text_parts), user_text):
                            who = self.settings.assistant.address_user_as
                            text_parts = [f"Sorry{', ' + who if who else ''}, I wasn't able to do that."]
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
                        conv.messages.append({"role": "user", "content":
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
                results = await asyncio.gather(*(
                    self.registry.execute(fn.get("name", ""), fn.get("arguments"), ctx)
                    for _, fn in named))
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

    async def _fast_command(self, conv: Conversation, intent: tuple[str, dict], ctx: ToolContext,
                            t0: float, timings: dict) -> AsyncIterator[Event]:
        name, args = intent
        cid = "fast_1"
        yield ToolStarted(cid, name, args)
        started = time.perf_counter()
        res = await self.registry.execute(name, args, ctx)
        timings["tools_ms"] = _ms(started)
        text = res.content if isinstance(res.content, str) else _summarize_content(res.content, 300)
        yield ToolFinished(cid, name, res.is_error, _summarize_content(text, 200), _ms(started))
        spoken = text.strip() or "Done."
        timings["first_token_ms"] = _ms(t0)
        yield TextDelta(spoken)
        # Record it in the conversation as plain text, so the model knows what happened.
        conv.messages.append({"role": "assistant", "content": spoken})
        conv.trim()
        timings["total_ms"] = _ms(t0)
        yield TurnComplete(spoken, timings, {"input_tokens": 0, "output_tokens": 0}, "fast_command")

    async def _model_round(self, conv: Conversation, allow_tools: bool, out: "_Round",
                           timings: dict, usage: dict, t0: float) -> AsyncIterator[TextDelta]:
        """One streamed model response: yields speakable text, collects tool calls in `out`."""
        think = ThinkFilter(hold=self._hold_think)
        async for chunk in self._stream(self._body(conv.messages, tools=allow_tools)):
            msg = chunk.get("message") or {}
            # msg["thinking"] (separated reasoning) is never spoken.
            text = think.feed(msg.get("content") or "")
            if chunk.get("done"):
                text += think.flush()
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
    r"|\b\w+ing\b[^.]{0,80}\b(?:is|are) (?:now )?done\b", re.I)

NUDGE = ("(Note from the system, not the user: you said you would do that but did not call a "
         "tool. Call the right tool now. If no tool can do it, say so in one short sentence.)")
NUDGE_CLAIM = ("(Note from the system, not the user: you said it was done, but you did not call any "
               "tool, so nothing happened. Call the right tools now; you can call several at once. "
               "If no tool can do part of it, say which part in one short sentence.)")


_ACTION_REQUEST = re.compile(
    r"\b(?:open|close|minimi[sz]e|maximi[sz]e|pause|unpause|resume|play|turn|set|mute|unmute|lock|"
    r"full\s?screen|start|stop|switch|launch|cancel|volume|skip|show|hide|search|remind)\b", re.I)
_QUESTION = re.compile(r"^\W*(?:is|are|was|were|does|do|did|when|what|where|why|how|which|who)\b", re.I)


def _acts_without_tools(text: str, request: str | None = None) -> bool:
    """Promised an action, or claimed one was done. A claim only counts when the user asked for
    an action ("is the shop open?" -> "it's closed on Sundays" is just an answer)."""
    if _PROMISE.search(text):
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
