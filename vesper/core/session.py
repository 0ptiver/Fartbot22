"""A client session: conversation state, the in-flight turn, and pending confirmations."""

from __future__ import annotations

import asyncio
import itertools
from typing import Any, Awaitable, Callable

from vesper.brain.llm import Brain, BrainError, event_to_dict
from vesper.core.conversation import Conversation
from vesper.tools.registry import ToolContext

Send = Callable[[dict[str, Any]], Awaitable[None]]


class Session:
    CONFIRM_TIMEOUT_S = 60

    def __init__(self, brain: Brain, send: Send, client_id: str = "local", remote: bool = False):
        self.brain = brain
        self.send = send
        self.conv = Conversation(brain.settings.brain.history_turns)
        self.ctx = ToolContext(brain.settings, client_id=client_id, remote=remote,
                               confirm=self._confirm, services={"brain": brain})
        self._turn: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._ids = itertools.count(1)

    # --- turns --------------------------------------------------------------
    def start_turn(self, text: str) -> asyncio.Task:
        self.cancel_turn()
        self._turn = asyncio.create_task(self._run(text))
        return self._turn

    async def _run(self, text: str) -> None:
        try:
            async for ev in self.brain.run_turn(self.conv, text, self.ctx):
                await self.send(event_to_dict(ev))
        except asyncio.CancelledError:
            await self.send({"type": "cancelled"})
            raise
        except Exception as e:  # never let a turn crash the connection
            await self.send(event_to_dict(BrainError(f"Internal error: {type(e).__name__}: {e}")))

    def cancel_turn(self) -> bool:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result(False)
        if self._turn and not self._turn.done():
            self._turn.cancel()
            return True
        return False

    async def wait_idle(self) -> None:
        if self._turn:
            try:
                await self._turn
            except asyncio.CancelledError:
                pass

    # --- confirmations --------------------------------------------------------
    async def _confirm(self, tool: str, args: dict[str, Any]) -> bool:
        cid = f"c{next(self._ids)}"
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        await self.send({"type": "confirm_request", "id": cid, "tool": tool, "input": args})
        try:
            return await asyncio.wait_for(fut, self.CONFIRM_TIMEOUT_S)
        except asyncio.TimeoutError:
            return False
        finally:
            self._pending.pop(cid, None)

    def resolve_confirm(self, cid: str, approved: bool) -> None:
        fut = self._pending.get(cid)
        if fut and not fut.done():
            fut.set_result(bool(approved))

    # --- inbound messages -----------------------------------------------------
    async def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "user_text" and str(msg.get("text", "")).strip():
            self.start_turn(str(msg["text"]).strip())
        elif kind == "confirm_response":
            self.resolve_confirm(str(msg.get("id")), bool(msg.get("approved")))
        elif kind == "cancel":
            self.cancel_turn()
        elif kind == "reset":
            self.cancel_turn()
            self.conv.clear()
            await self.send({"type": "reset_done"})
        else:
            await self.send({"type": "error", "message": f"unknown message type: {kind}", "recoverable": True})
