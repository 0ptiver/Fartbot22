"""Claude brain: streaming responses, manual tool loop, prompt caching, expert hand-off."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import anthropic

from vesper.brain.prompts import EXPERT_SYSTEM, system_prompt, turn_context
from vesper.core.config import Settings
from vesper.core.conversation import Conversation
from vesper.core.secrets import get_secret
from vesper.tools.registry import ToolContext, ToolRegistry, _summarize_content

CACHE = {"type": "ephemeral"}


# --- events streamed to clients --------------------------------------------
@dataclass
class TextDelta:
    text: str
    type: str = "text"


@dataclass
class ToolStarted:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_started"


@dataclass
class ToolFinished:
    id: str
    name: str
    is_error: bool
    summary: str
    duration_ms: int
    type: str = "tool_finished"


@dataclass
class TurnComplete:
    text: str
    timings: dict[str, float]
    usage: dict[str, int]
    stop_reason: str | None
    type: str = "turn_complete"


@dataclass
class BrainError:
    message: str
    recoverable: bool = True
    type: str = "error"


Event = TextDelta | ToolStarted | ToolFinished | TurnComplete | BrainError


@dataclass
class _Usage:
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, usage: Any) -> None:
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens"):
            self.counts[k] = self.counts.get(k, 0) + (getattr(usage, k, 0) or 0)


def _dump(block: Any) -> dict[str, Any]:
    return block.model_dump(mode="json", exclude_none=True) if hasattr(block, "model_dump") else block


class Brain:
    def __init__(self, settings: Settings, registry: ToolRegistry,
                 client: anthropic.AsyncAnthropic | None = None):
        self.settings = settings
        self.registry = registry
        self._client = client
        self._system = [{"type": "text", "text": system_prompt(settings), "cache_control": CACHE}]

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            key = get_secret("ANTHROPIC_API_KEY")
            if not key:
                raise RuntimeError(
                    "No ANTHROPIC_API_KEY. Put it in config/.env or run "
                    "`python -m vesper secrets set ANTHROPIC_API_KEY`."
                )
            self._client = anthropic.AsyncAnthropic(api_key=key)
        return self._client

    def tools(self) -> list[dict[str, Any]]:
        tools = self.registry.definitions()
        ws = self.settings.brain.web_search
        if ws.enabled:
            tools.append({"type": "web_search_20250305", "name": "web_search", "max_uses": ws.max_uses})
        return tools

    # --- main loop ----------------------------------------------------------
    async def run_turn(self, conv: Conversation, user_text: str, ctx: ToolContext,
                       extra_context: dict[str, str] | None = None) -> AsyncIterator[Event]:
        cfg = self.settings.brain
        ctx.services.setdefault("brain", self)
        t0 = time.perf_counter()
        timings: dict[str, float] = {}
        usage = _Usage()
        spoken: list[str] = []
        stop_reason: str | None = None
        checkpoint = conv.checkpoint()
        conv.add_user(user_text, turn_context(self.settings, extra_context))

        try:
            rounds = 0
            json_retries = 0
            while True:
                allow_tools = rounds < cfg.max_tool_rounds
                req: dict[str, Any] = dict(
                    model=cfg.chat_model,
                    max_tokens=cfg.chat_max_tokens,
                    system=self._system,
                    messages=conv.messages,
                    cache_control=CACHE,  # auto-cache the growing conversation prefix
                    tools=self.tools(),
                )
                if not allow_tools:  # safety cap reached: force a spoken answer
                    req["tool_choice"] = {"type": "none"}
                try:
                    async with self.client.messages.stream(**req) as stream:
                        async for ev in stream:
                            if ev.type == "text":
                                if "first_token_ms" not in timings:
                                    timings["first_token_ms"] = _ms(t0)
                                spoken.append(ev.text)
                                yield TextDelta(ev.text)
                            elif (ev.type == "content_block_start"
                                  and ev.content_block.type == "server_tool_use"):
                                yield ToolStarted(ev.content_block.id, ev.content_block.name, {})
                        response = await stream.get_final_message()
                    json_retries = 0
                except ValueError:
                    # Tool input JSON the SDK could not parse at all; re-issue the round.
                    json_retries += 1
                    if json_retries > 2:
                        raise
                    continue

                usage.add(response.usage)
                stop_reason = response.stop_reason
                conv.add_assistant([_dump(b) for b in response.content])

                for b in response.content:
                    if b.type == "web_search_tool_result":
                        err = not isinstance(b.content, list)
                        yield ToolFinished(b.tool_use_id, "web_search", err,
                                           "search failed" if err else f"{len(b.content)} results", 0)

                if stop_reason == "pause_turn":
                    continue
                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if stop_reason == "refusal" or not tool_uses:
                    break

                rounds += 1
                if stop_reason == "max_tokens":
                    # Truncated tool input: never run it; tell the model and let it retry.
                    conv.add_tool_results([
                        {"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                         "content": "Tool input was cut off (max_tokens). Try a shorter call."}
                        for b in tool_uses
                    ])
                    continue

                results = []
                for b in tool_uses:
                    yield ToolStarted(b.id, b.name, b.input if isinstance(b.input, dict) else {})
                started = time.perf_counter()
                outcomes = await asyncio.gather(
                    *(self.registry.execute(b.name, b.input, ctx) for b in tool_uses)
                )
                timings.setdefault("tools_ms", 0)
                timings["tools_ms"] += _ms(started)
                for b, res in zip(tool_uses, outcomes):
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": res.content, "is_error": res.is_error})
                    yield ToolFinished(b.id, b.name, res.is_error,
                                       _summarize_content(res.content, 200), _ms(started))
                conv.add_tool_results(results)
                if spoken and not spoken[-1].endswith((" ", "\n")):
                    spoken.append(" ")

        except asyncio.CancelledError:
            # Barge-in / kill switch: keep a valid history containing what was said.
            conv.rollback(checkpoint)
            conv.add_user(user_text)
            said = "".join(spoken).strip()
            conv.add_assistant([{"type": "text", "text": (said + " [interrupted]") if said else "[interrupted]"}])
            raise
        except anthropic.APIConnectionError:
            conv.rollback(checkpoint)
            yield BrainError("I can't reach my brain at the moment. The internet may be down.")
            return
        except anthropic.RateLimitError:
            conv.rollback(checkpoint)
            yield BrainError("I'm being rate limited. Give me a moment.")
            return
        except anthropic.AuthenticationError:
            conv.rollback(checkpoint)
            yield BrainError("My API key was rejected. Please check ANTHROPIC_API_KEY.", recoverable=False)
            return
        except anthropic.APIStatusError as e:
            conv.rollback(checkpoint)
            yield BrainError(f"The model API returned an error ({e.status_code}).")
            return
        except RuntimeError as e:
            conv.rollback(checkpoint)
            yield BrainError(str(e), recoverable=False)
            return

        conv.trim()
        timings["total_ms"] = _ms(t0)
        yield TurnComplete("".join(spoken).strip(), timings, usage.counts, stop_reason)

    # --- expert hand-off ----------------------------------------------------
    async def ask_expert(self, task: str, context: str = "") -> str:
        cfg = self.settings.brain
        prompt = f"{context}\n\nTask: {task}" if context else task
        async with self.client.beta.messages.stream(
            model=cfg.expert_model,
            max_tokens=cfg.expert_max_tokens,
            system=EXPERT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            thinking={"type": "adaptive"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            msg = await stream.get_final_message()
        if msg.stop_reason == "refusal":
            return "The expert model declined this request."
        return "".join(b.text for b in msg.content if b.type == "text").strip() or "(no answer)"


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def event_to_dict(ev: Event) -> dict[str, Any]:
    return json.loads(json.dumps(ev.__dict__, default=str))
