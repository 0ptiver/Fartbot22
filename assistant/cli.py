"""Terminal chat client: in-process (--local) or over the server's WebSocket."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

DIM, CYAN, YELLOW, RED, RESET = "\033[2m", "\033[36m", "\033[33m", "\033[31m", "\033[0m"


async def _ainput(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


class Printer:
    """Renders server events. Shared by the local and WebSocket modes."""

    def __init__(self, name: str, debug: bool):
        self.name = name
        self.debug = debug
        self.started = False

    def __call__(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        if t == "text":
            if not self.started:
                print(f"{CYAN}{self.name}:{RESET} ", end="")
                self.started = True
            print(ev["text"], end="", flush=True)
        elif t == "tool_started":
            args = json.dumps(ev.get("input") or {})
            print(f"\n{DIM}  ⚙ {ev['name']} {args}{RESET}", flush=True)
            self.started = False
        elif t == "tool_finished":
            mark = f"{RED}✗" if ev["is_error"] else "✓"
            print(f"{DIM}  {mark} {ev['name']}: {ev['summary'][:120]}{RESET}", flush=True)
        elif t == "turn_complete":
            print()
            if self.debug:
                u = ev["usage"]
                print(f"{DIM}  [{ev['timings']}  in={u.get('input_tokens')} out={u.get('output_tokens')} "
                      f"cache_read={u.get('cache_read_input_tokens')} "
                      f"cache_write={u.get('cache_creation_input_tokens')}]{RESET}")
            self.started = False
        elif t == "error":
            print(f"\n{RED}{ev['message']}{RESET}")
            self.started = False
        elif t == "cancelled":
            print(f"\n{DIM}  (interrupted){RESET}")
            self.started = False


async def run_local(debug: bool) -> None:
    from assistant.brain.llm import Brain
    from assistant.core.config import load_settings
    from assistant.core.session import Session
    from assistant.tools import build_registry

    settings = load_settings()
    brain = Brain(settings, build_registry(settings))
    printer = Printer(settings.assistant.name, debug)
    session: Session

    async def send(ev: dict[str, Any]) -> None:
        if ev["type"] == "confirm_request":
            answer = await _ainput(f"\n{YELLOW}Allow {ev['tool']} {json.dumps(ev['input'])}? [y/N] {RESET}")
            session.resolve_confirm(ev["id"], answer.strip().lower() in ("y", "yes"))
        else:
            printer(ev)

    session = Session(brain, send, client_id="cli")
    print(f"{settings.assistant.name} (local, model {settings.brain.chat_model}). "
          "Type /reset, /quit. Ctrl+C interrupts a reply.")
    await _repl(session.handle, session.wait_idle, session.cancel_turn)


async def run_remote(url: str, debug: bool) -> None:
    import websockets

    from assistant.core.secrets import local_client_token

    headers = {"Authorization": f"Bearer {local_client_token()}"}
    async with websockets.connect(url, max_size=None, additional_headers=headers) as ws:
        hello = json.loads(await ws.recv())
        printer = Printer(hello.get("name", "Assistant"), debug)
        idle = asyncio.Event()
        idle.set()

        async def reader() -> None:
            async for raw in ws:
                ev = json.loads(raw)
                if ev["type"] == "confirm_request":
                    answer = await _ainput(f"\n{YELLOW}Allow {ev['tool']} {json.dumps(ev['input'])}? [y/N] {RESET}")
                    await ws.send(json.dumps({"type": "confirm_response", "id": ev["id"],
                                              "approved": answer.strip().lower() in ("y", "yes")}))
                    continue
                printer(ev)
                if ev["type"] in ("turn_complete", "error", "cancelled", "reset_done"):
                    idle.set()

        task = asyncio.create_task(reader())

        async def handle(msg: dict[str, Any]) -> None:
            idle.clear()
            await ws.send(json.dumps(msg))

        async def cancel() -> bool:
            await ws.send(json.dumps({"type": "cancel"}))
            return True

        print(f"Connected to {url}. Type /reset, /quit.")
        try:
            await _repl(handle, idle.wait, cancel)
        finally:
            task.cancel()


async def _repl(handle, wait_idle, cancel) -> None:
    while True:
        try:
            line = (await _ainput("\nyou> ")).strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return
        if line == "/reset":
            await handle({"type": "reset"})
            await wait_idle()
            continue
        await handle({"type": "user_text", "text": line})
        try:
            await wait_idle()
        except (KeyboardInterrupt, asyncio.CancelledError):
            r = cancel()
            if asyncio.iscoroutine(r):
                await r


def main(argv: list[str]) -> None:
    import argparse

    p = argparse.ArgumentParser(prog="assistant chat")
    p.add_argument("--local", action="store_true", help="run the brain in-process (no server)")
    p.add_argument("--url", default=None, help="server WebSocket URL")
    p.add_argument("--debug", action="store_true", help="show timings and token usage")
    a = p.parse_args(argv)
    if a.local:
        asyncio.run(run_local(a.debug))
    else:
        from assistant.core.config import load_settings

        s = load_settings()
        asyncio.run(run_remote(a.url or f"ws://{s.server.host}:{s.server.port}/ws", a.debug))


if __name__ == "__main__":
    main(sys.argv[1:])
