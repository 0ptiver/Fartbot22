"""The HUD: a small local web app that shows and steers the running voice loop.

It runs inside the `assistant voice` process (same asyncio loop), so it sees every event
the voice loop emits and can press its buttons directly.

Security:
- listens on 127.0.0.1 only and refuses non-loopback peers;
- a new random key every run, handed to the window in the URL *fragment* (never sent
  over HTTP, never logged); the page must present it within 5 s of connecting;
- Host header must be 127.0.0.1/localhost:<port> (stops DNS rebinding) and the WebSocket
  Origin must be the HUD's own page, so no website you visit can talk to it;
- strict Content-Security-Policy: only the HUD's own scripts run, nothing from the web;
- everything shown comes from speech/tools, so the page only ever uses textContent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import socket
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from assistant.core.config import ROOT, Settings
from assistant.core.server import _is_loopback, _token_ok

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
URL_FILE = ROOT / "data" / "hud-url.txt"
AUTH_TIMEOUT_S = 5
STATUS_EVERY_S = 0.1
# Events worth replaying to a window that (re)connects.
_KEEP = {"transcript", "text", "speak", "tool", "tool_done", "announcement", "confirm_request",
         "dictation", "dictated",
         "confirm_result", "standby", "error", "interrupted", "stopped", "merged", "ignored",
         "turn_complete", "latency"}
_FILES = {"/": ("index.html", "text/html"), "/hud.js": ("hud.js", "text/javascript"),
          "/hud.css": ("hud.css", "text/css"), "/icon.svg": ("icon.svg", "image/svg+xml")}


class Hub:
    """Fan-out of voice events to connected HUD windows, with a short history."""

    def __init__(self, backlog: int = 400):
        self.clients: set[asyncio.Queue] = set()
        self.backlog: deque[dict] = deque(maxlen=backlog)

    def publish(self, ev: dict[str, Any]) -> None:
        if ev.get("type") in _KEEP:
            self.backlog.append({**ev, "ts": time.time()})
        for q in list(self.clients):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:     # a stuck window must never slow the voice loop
                pass


def status(loop, settings: Settings) -> dict[str, Any]:
    pending = loop.confirm_text if getattr(loop, "_confirm", None) is not None else None
    return {"type": "status", "state": loop.state, "level": round(min(loop.mic_level * 6, 1.0), 3),
            "standby": loop.standby, "muted": loop.mic_muted, "mode": settings.voice.mode,
            "dictation": bool(getattr(loop, "dictation", False)),
            "confirm": pending}


def timers(scheduler, settings: Settings) -> dict[str, Any]:
    items = [] if scheduler is None else [
        {"id": r.id, "kind": r.kind, "text": r.text, "due": r.due} for r in scheduler.upcoming()]
    return {"type": "timers", "items": items, "now": time.time()}


def _routines(settings: Settings) -> list[dict[str, str]]:
    from assistant.tools.routines import active
    return [{"name": n, "label": n.replace("_", " ").capitalize(), "phrase": (r.phrases or [n])[0]}
            for n, r in active(settings).items()]


def create_hud_app(settings: Settings, loop, hub: Hub, token: str, scheduler=None,
                   trust_test_client: bool = False) -> FastAPI:
    port = settings.hud.port
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}
    csp = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
           f"connect-src 'self' ws://127.0.0.1:{port} ws://localhost:{port}; "
           "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    tasks: set[asyncio.Task] = set()

    def peer_ok(host: str | None) -> bool:
        return _is_loopback(host) or (trust_test_client and host == "testclient")

    def spawn(coro) -> None:
        t = asyncio.ensure_future(coro)
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        client = request.client.host if request.client else None
        if not peer_ok(client) or request.headers.get("host", "") not in hosts:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        resp: Response = await call_next(request)
        resp.headers.update({"Content-Security-Policy": csp, "X-Content-Type-Options": "nosniff",
                             "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
                             "X-Frame-Options": "DENY"})
        return resp

    for path, (name, media) in _FILES.items():
        def make(name=name, media=media):
            async def serve() -> FileResponse:
                return FileResponse(STATIC / name, media_type=media)
            return serve
        app.get(path)(make())

    async def handle(msg: dict[str, Any], ws_send) -> None:
        kind = msg.get("type")
        if kind == "text" and isinstance(msg.get("text"), str):
            spawn(loop.submit_text(msg["text"][:2000]))
        elif kind == "confirm":
            loop.answer_confirm(msg.get("approved") is True)       # anything but true is a no
        elif kind == "stand_down":
            spawn(loop.stand_down())
        elif kind == "resume":
            spawn(loop.resume())
        elif kind == "stop":
            spawn(loop.interrupt(reason="stop button"))
        elif kind == "dictation" and hasattr(loop, "set_dictation"):
            loop.set_dictation(bool(msg.get("on")))
        elif kind == "mute":
            loop.mic_muted = bool(msg.get("on"))
        elif kind == "mode" and msg.get("mode") in ("wake", "open_mic"):
            settings.voice.mode = msg["mode"]                       # until Nova restarts
        elif kind == "cancel_timer" and scheduler is not None and isinstance(msg.get("id"), str):
            if any(r.id == msg["id"] for r in scheduler.upcoming()):
                scheduler.cancel(msg["id"])
            await ws_send(timers(scheduler, settings))
        elif kind == "routine":
            r = next((r for r in _routines(settings) if r["name"] == msg.get("name")), None)
            if r is not None:
                spawn(loop.submit_text(r["phrase"]))

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        client = websocket.client.host if websocket.client else None
        if (not peer_ok(client) or websocket.headers.get("host", "") not in hosts
                or websocket.headers.get("origin") not in origins):
            await websocket.close(code=4403)
            return
        await websocket.accept()
        try:
            first = await asyncio.wait_for(websocket.receive_json(), AUTH_TIMEOUT_S)
        except Exception:
            first = None
        if not (isinstance(first, dict) and first.get("type") == "auth"
                and _token_ok(str(first.get("token", "")), token)):
            await websocket.close(code=4401)
            return

        lock = asyncio.Lock()

        async def send(msg: dict[str, Any]) -> None:
            async with lock:
                await websocket.send_text(json.dumps(msg, default=str))

        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        await send({"type": "hello", "name": settings.assistant.name,
                    "user": settings.assistant.address_user_as, "routines": _routines(settings),
                    "confirm_timeout_s": settings.voice.confirm_timeout_s,
                    "show_ignored": settings.hud.show_ignored, "backlog": list(hub.backlog)})
        await send(timers(scheduler, settings))
        hub.clients.add(queue)

        async def pump() -> None:
            last_tick = 0.0
            last_timers = 0.0
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), STATUS_EVERY_S)
                    await send(ev)
                except asyncio.TimeoutError:
                    pass
                now = time.monotonic()
                if now - last_tick >= STATUS_EVERY_S:
                    last_tick = now
                    await send(status(loop, settings))
                if now - last_timers >= 1.0:
                    last_timers = now
                    await send(timers(scheduler, settings))

        pumper = asyncio.create_task(pump())
        try:
            while True:
                raw = await websocket.receive_text()
                if len(raw) > 8000:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await handle(msg, send)
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(queue)
            pumper.cancel()
            await asyncio.gather(pumper, return_exceptions=True)

    return app


# --- running inside the voice process --------------------------------------------------------
class _QuietServer:
    """uvicorn without its Ctrl+C handling: Ctrl+C belongs to the voice loop."""

    def __init__(self, app: FastAPI, sock: socket.socket):
        import uvicorn

        class Server(uvicorn.Server):
            @contextlib.contextmanager
            def capture_signals(self):
                yield

            def install_signal_handlers(self) -> None:   # older uvicorn
                pass

        self.sock = sock
        self.server = Server(uvicorn.Config(app, log_level="warning", lifespan="off"))

    async def serve(self) -> None:
        await self.server.serve(sockets=[self.sock])

    def stop(self) -> None:
        self.server.should_exit = True


class HudHandle:
    def __init__(self, url: str, task: asyncio.Task, server: _QuietServer):
        self.url, self.task, self.server = url, task, server

    async def close(self) -> None:
        self.server.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.task, 3)
        with contextlib.suppress(OSError):
            URL_FILE.unlink()


def _bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):            # Windows: nobody else can share the port
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(16)
    sock.setblocking(False)
    return sock


async def start_hud(settings: Settings, loop, hub: Hub, scheduler=None) -> HudHandle | None:
    """Serve the HUD on 127.0.0.1 and (optionally) open its window. None if it can't start."""
    try:
        sock = _bind(settings.hud.port)
    except OSError as e:
        log.warning("HUD port %s busy: %s", settings.hud.port, e)
        return None
    token = secrets.token_urlsafe(32)
    app = create_hud_app(settings, loop, hub, token, scheduler)
    server = _QuietServer(app, sock)
    task = asyncio.create_task(server.serve())
    url = f"http://127.0.0.1:{settings.hud.port}/#k={token}"
    try:
        URL_FILE.parent.mkdir(parents=True, exist_ok=True)
        URL_FILE.write_text(url, encoding="utf-8")         # for `assistant hud` (reopen the window)
    except OSError:
        pass
    if settings.hud.open_window:
        open_window(url)
    return HudHandle(url, task, server)


def _find_browser() -> str | None:
    import os
    import shutil

    for exe in ("msedge", "chrome"):
        found = shutil.which(exe)
        if found:
            return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LocalAppData")):
        if not base:
            continue
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            p = Path(base) / rel
            if p.exists():
                return str(p)
    return None


def window_command(browser: str, url: str) -> list[str]:
    # Its own browser profile: no extensions, no shared cookies, nothing else can read the page.
    profile = ROOT / "data" / "hud-browser"
    return [browser, f"--app={url}", f"--user-data-dir={profile}", "--window-size=460,820",
            "--no-first-run", "--no-default-browser-check", "--disable-extensions",
            "--disable-sync", "--disable-background-networking"]


def open_window(url: str) -> bool:
    from assistant.core.launch import launch, launch_command

    browser = _find_browser()
    try:
        if browser:
            launch_command(window_command(browser, url))
        else:
            launch(url)
        return True
    except Exception as e:
        log.warning("couldn't open the HUD window: %s", e)
        return False
