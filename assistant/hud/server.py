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
_KEEP = {"memory_used", "transcript", "text", "speak", "tool", "tool_done", "announcement", "confirm_request",
         "dictation", "dictated",
         "confirm_result", "standby", "error", "interrupted", "stopped", "merged", "ignored",
         "turn_complete", "latency", "remote"}
_FILES = {"/": ("index.html", "text/html"), "/hud.js": ("hud.js", "text/javascript"),
          "/hud.css": ("hud.css", "text/css"), "/icon.svg": ("icon.svg", "image/svg+xml"),
          "/brain.js": ("brain.js", "text/javascript"), "/voice.js": ("voice.js", "text/javascript"),
          "/phonesetup.js": ("phonesetup.js", "text/javascript"), "/orb.js": ("orb.js", "text/javascript"),
          "/home.js": ("home.js", "text/javascript")}


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
            "lock": _lock_status(getattr(loop, "lock", None)),
            "subtitles": bool(getattr(getattr(loop, "ctx", None), "services", {}).get("subtitles") and
                              loop.ctx.services["subtitles"].on),
            "confirm": pending}


def _lock_status(lock) -> dict | None:
    if lock is None:
        return None
    return {"on": bool(lock.on), "enrolled": lock.prints.enrolled, "enrolling": lock.enrolling is not None,
            "score": None if lock.last_score is None else round(lock.last_score, 2),
            "threshold": round(lock.threshold, 2)}


def timers(scheduler, settings: Settings, watchers=None) -> dict[str, Any]:
    items = [] if scheduler is None else [
        {"id": r.id, "kind": r.kind, "text": r.text, "due": r.due, "created": r.created}
        for r in scheduler.upcoming()]
    watching = [] if watchers is None else [{"id": w.id, "text": w.describe()} for w in watchers.items.values()]
    return {"type": "timers", "items": items, "watches": watching, "now": time.time(),
            "routines": _routines(settings)}


MEDIA_EVERY_S = 2.0
MEDIA_WAIT_S = 5.0          # longer than the media list's own limit (video.ASK_S * 2), so it decides
_HUD_MEDIA = None           # its own reader: the tools' reader keeps a session list they index into


async def now_playing_items() -> list[dict] | None:
    """What Windows says is playing (the volume pop-up's list), for the Now playing card.
    None when Nova can't see it (not Windows)."""
    global _HUD_MEDIA
    from assistant.tools import video
    if _HUD_MEDIA is None:
        _HUD_MEDIA = video.MediaBackend()
    if not _HUD_MEDIA.available():
        return None
    try:
        items = await _HUD_MEDIA.list()
    except Exception:
        return None
    rank = {"playing": 0, "paused": 1}
    items = sorted(items, key=lambda m: rank.get(m.status, 2))
    return [{"title": m.title, "artist": m.artist, "status": m.status, "app": _app_label(m.app)}
            for m in items[:3] if m.title]


def _app_label(app_id: str) -> str:
    """'Spotify.exe' -> 'Spotify', 'firefox.exe' -> 'Firefox'. Firefox reports a 16-character
    code instead of its name."""
    from assistant.tools import video
    a = app_id.lower()
    if "spotify" in a:
        return "Spotify"
    for b in video.BROWSERS:
        if b in a:
            return {"msedge": "Edge"}.get(b, b.capitalize())
    if len(app_id) == 16 and all(c in "0123456789ABCDEF" for c in app_id):
        return "Firefox"
    return app_id.removesuffix(".exe").split("!")[-1][:20] or "Player"


_VITALS: dict[str, Any] = {"at": 0.0, "ev": None}
VITALS_EVERY_S = 3.0


def vitals(_gpu=None) -> dict[str, Any]:
    """CPU, RAM, GPU temperature and VRAM for the HUD's "This PC" panel. Cached for a few
    seconds so several windows don't each start nvidia-smi."""
    now = time.monotonic()
    if _VITALS["ev"] is not None and now - _VITALS["at"] < VITALS_EVERY_S - 0.1:
        return _VITALS["ev"]
    ev: dict[str, Any] = {"type": "vitals", "cpu": None, "ram": None, "gpu_temp": None, "vram": None}
    try:
        import psutil
        ev["cpu"] = round(psutil.cpu_percent(interval=None))
        ev["ram"] = round(psutil.virtual_memory().percent)
    except Exception:
        pass
    if _gpu is None:
        from assistant.tools.pc import gpu_stats as _gpu
    g = _gpu()
    if g:
        ev["gpu_temp"] = g["temp"]
        ev["vram"] = round(100 * g["mem_used"] / max(g["mem_total"], 1))
    _VITALS.update(at=now, ev=ev)
    return ev


def lessons_event() -> dict[str, Any]:
    from assistant.brain.lessons import get_lessons
    return {"type": "lessons", "items": [{"id": x.id, "said": x.said, "what": x.describe(), "uses": x.uses,
                                          "created": x.created} for x in reversed(get_lessons().items())]}


def memories(store) -> dict[str, Any]:
    items = [] if store is None else [{"id": m.id, "text": m.text, "created": m.created} for m in store.all()]
    return {"type": "memories", "items": items}


PREVIEW_LINE = "Good evening, sir. Everything is running smoothly, and your tea is on its way."


def voice_info(loop) -> dict[str, Any]:
    from dataclasses import asdict

    from assistant.voice.voicedesign import PRESETS, catalog
    tts = getattr(loop, "tts", None)
    if tts is None or not hasattr(tts, "voices") or not tts.voices():
        return {"type": "voice", "unavailable": True}
    return {"type": "voice", "voices": catalog(tts.voices()), "design": asdict(tts.design), "presets": PRESETS}


def _routines(settings: Settings) -> list[dict[str, str]]:
    from assistant.tools.routines import active
    from assistant.tools.teach import load_learned
    learned = load_learned()
    return [{"name": n, "label": n.replace("_", " ").capitalize(), "phrase": (r.phrases or [n])[0],
             "learned": n in learned and n not in settings.routines}
            for n, r in active(settings).items()]


def create_hud_app(settings: Settings, loop, hub: Hub, token: str, scheduler=None,
                   trust_test_client: bool = False, memory=None, watchers=None, phone=None) -> FastAPI:
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
            await ws_send(timers(scheduler, settings, watchers))
        elif kind == "cancel_watch" and watchers is not None and isinstance(msg.get("id"), str):
            watchers.cancel(msg["id"])
            await ws_send(timers(scheduler, settings, watchers))
        elif kind in ("voice_preview", "voice_save") and isinstance(msg.get("design"), dict):
            from assistant.voice.voicedesign import VoiceDesign

            async def voice_job() -> None:
                try:
                    design = VoiceDesign.from_dict(msg["design"])
                    if kind == "voice_preview":
                        text = msg.get("text") if isinstance(msg.get("text"), str) and msg["text"].strip() else PREVIEW_LINE
                        await loop.preview_voice(design, text[:200])
                    else:
                        await loop.set_voice(design)
                        await ws_send(voice_info(loop))
                        await ws_send({"type": "toast", "text": "Saved. That's Nova's voice now."})
                except Exception as e:
                    await ws_send({"type": "toast", "text": f"Couldn't change the voice: {e}"})
            spawn(voice_job())
        elif kind == "subtitles":
            subs = getattr(getattr(loop, "ctx", None), "services", {}).get("subtitles")
            if subs is None:
                await ws_send({"type": "toast", "text": "Subtitles only work on Windows, in voice mode."})
            else:
                async def subs_job() -> None:
                    try:
                        await (subs.start() if msg.get("on") else subs.stop())
                    except Exception as e:
                        await ws_send({"type": "toast", "text": str(e)})
                spawn(subs_job())
        elif kind == "voice_lock" and getattr(loop, "lock", None) is not None:
            lock, a = loop.lock, msg.get("action")
            if a == "learn":
                spawn(loop.submit_text("learn my voice"))
            elif a in ("on", "off"):
                lock.set(on=a == "on")
            elif a == "forget":
                lock.forget()
                await ws_send({"type": "toast", "text": "Your voiceprint is deleted and voice lock is off."})
            elif a == "strictness" and isinstance(msg.get("value"), (int, float)):
                lock.set(threshold=float(msg["value"]))
        elif kind == "media_cmd" and msg.get("action") in ("play", "pause", "next", "previous"):
            registry = getattr(getattr(loop, "brain", None), "registry", None)
            if registry is not None:
                async def media_job(action=msg["action"]) -> None:
                    res = await registry.execute("media", {"action": action}, loop.ctx)
                    if res.is_error:
                        await ws_send({"type": "toast", "text": str(res.content)})
                spawn(media_job())
        elif kind == "voice_accept" and hasattr(loop, "accept_rejected"):
            async def accept_job() -> None:
                await ws_send({"type": "toast", "text": await loop.accept_rejected()})
            spawn(accept_job())
        elif kind == "voice_get":
            await ws_send(voice_info(loop))
        elif isinstance(kind, str) and kind.startswith("phone_") and phone is not None:
            await phone_message(kind, msg, ws_send)
        elif kind == "mem_add" and memory is not None and isinstance(msg.get("text"), str):
            from assistant.core.memory import SecretRefused
            try:
                await asyncio.to_thread(memory.add, msg["text"][:300])
            except (SecretRefused, ValueError) as e:
                await ws_send({"type": "toast", "text": str(e)})
        elif kind == "lesson_delete" and isinstance(msg.get("id"), str):
            from assistant.brain.lessons import get_lessons
            get_lessons().delete(msg["id"])
        elif kind == "mem_delete" and memory is not None and isinstance(msg.get("id"), int):
            await asyncio.to_thread(memory.delete, [msg["id"]])
        elif kind == "routine_delete" and isinstance(msg.get("name"), str):
            from assistant.tools.teach import delete_learned, load_learned
            if msg["name"] in load_learned():
                await ws_send({"type": "toast", "text": delete_learned(msg["name"].replace("_", " "))})
            await ws_send(timers(scheduler, settings, watchers))
        elif kind == "routine":
            r = next((r for r in _routines(settings) if r["name"] == msg.get("name")), None)
            if r is not None:
                spawn(loop.submit_text(r["phrase"]))

    async def phone_message(kind: str, msg: dict[str, Any], ws_send) -> None:
        """The Phone page. Set-up secrets go in (password, first code); they never come back out."""
        auth = phone.auth
        if kind == "phone_setup" and isinstance(msg.get("password"), str):
            try:
                shown = await asyncio.to_thread(auth.begin_setup, msg["password"][:200],
                                                f"{settings.assistant.name} on {socket.gethostname()}")
            except ValueError as e:
                await ws_send({"type": "toast", "text": str(e)})
                return
            await ws_send({"type": "phone_setup", **shown})
        elif kind == "phone_confirm" and isinstance(msg.get("code"), str):
            if await asyncio.to_thread(auth.finish_setup, msg["code"]):
                phone.chats.clear()
                phone.poke()
                await ws_send({"type": "phone_done"})
                await ws_send({"type": "toast", "text": "Phone access is set up and switched on."})
            else:
                await ws_send({"type": "toast", "text": "That code didn't match. Type the code your app shows now "
                                                        "(it changes every 30 seconds)."})
        elif kind == "phone_cancel":
            auth.cancel_setup()
        elif kind == "phone_enable":
            auth.set_enabled(msg.get("on") is True)
            phone.poke()
            await asyncio.sleep(0.3)
        elif kind == "phone_revoke":
            device = msg.get("id") if isinstance(msg.get("id"), str) else None
            auth.revoke(device)
            for d in ([device] if device else list(phone.chats)):
                chat = phone.chats.pop(d, None)
                if chat is not None:
                    chat.session.cancel_turn()
            await ws_send({"type": "toast", "text": "Signed out." if device else "Every phone is signed out."})
        elif kind == "phone_get":
            await phone.refresh_tailscale()
        elif kind == "phone_reset":
            auth.reset()
            phone.poke()
            await ws_send({"type": "phone_done"})
            await ws_send({"type": "toast", "text": "Phone access is off and forgotten."})
        await ws_send(phone.info())

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
        await send(timers(scheduler, settings, watchers))
        await send(memories(memory))
        await send(lessons_event())
        hub.clients.add(queue)

        async def pump() -> None:
            last_tick = 0.0
            last_timers = 0.0
            last_vitals = 0.0
            last_media, media_sent = 0.0, None
            media_job: asyncio.Task | None = None
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
                    await send(timers(scheduler, settings, watchers))
                if now - last_media >= MEDIA_EVERY_S and (media_job is None or media_job.done()):
                    last_media = now
                    # Asked on the side with a time limit: if Windows' media list hangs, the rest
                    # of the window keeps moving (owner: "now the hud is not moving").
                    media_job = asyncio.create_task(asyncio.wait_for(now_playing_items(), MEDIA_WAIT_S))
                    # Its outcome is always collected, even if this window closes first (owner's log:
                    # "Task exception was never retrieved ... TimeoutError").
                    media_job.add_done_callback(lambda t: t.cancelled() or t.exception())
                if media_job is not None and media_job.done():
                    job, media_job = media_job, None
                    items = None if job.cancelled() or job.exception() else job.result()
                    if items is not None and items != media_sent:
                        media_sent = items
                        await send({"type": "media", "items": items})
                if now - last_vitals >= VITALS_EVERY_S:
                    last_vitals = now
                    await send(await asyncio.to_thread(vitals))
                    if phone is not None:
                        await send(phone.info())

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

    def __init__(self, app: FastAPI, sock: socket.socket, ssl: tuple[str, str] | None = None):
        import uvicorn

        class Server(uvicorn.Server):
            @contextlib.contextmanager
            def capture_signals(self):
                yield

            def install_signal_handlers(self) -> None:   # older uvicorn
                pass

        self.sock = sock
        tls = {"ssl_certfile": ssl[0], "ssl_keyfile": ssl[1]} if ssl else {}
        self.server = Server(uvicorn.Config(app, log_level="warning", lifespan="off", **tls))

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


def watch_memory(store, hub: Hub, aio_loop: asyncio.AbstractEventLoop) -> None:
    """Memory changes (from a tool thread or the HUD) reach every window; so do 'used' pulses."""
    def changed() -> None:
        aio_loop.call_soon_threadsafe(hub.publish, memories(store))

    def used(ids: list[int]) -> None:
        if ids:
            aio_loop.call_soon_threadsafe(hub.publish, {"type": "memory_used", "ids": ids})
    store.listeners.append(changed)
    store.used_listeners.append(used)


async def start_hud(settings: Settings, loop, hub: Hub, scheduler=None, memory=None,
                    watchers=None, phone=None) -> HudHandle | None:
    """Serve the HUD on 127.0.0.1 and (optionally) open its window. None if it can't start."""
    try:
        sock = _bind(settings.hud.port)
    except OSError as e:
        log.warning("HUD port %s busy: %s", settings.hud.port, e)
        return None
    token = secrets.token_urlsafe(32)
    if memory is None:
        from assistant.core.memory import get_store
        memory = get_store(settings)
    if memory is not None:
        watch_memory(memory, hub, asyncio.get_running_loop())
    from assistant.brain.lessons import get_lessons
    aio = asyncio.get_running_loop()
    get_lessons().listeners.append(lambda: aio.call_soon_threadsafe(hub.publish, lessons_event()))
    app = create_hud_app(settings, loop, hub, token, scheduler, memory=memory, watchers=watchers, phone=phone)
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
    return [browser, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1180,760",
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
