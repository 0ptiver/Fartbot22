"""Nova on your phone, over Tailscale (a private network between your own devices).

Safety, from the outside in:
- It only listens on the PC's Tailscale address (100.x.y.z), never on the home Wi-Fi or the
  internet, and only answers devices on your tailnet. No ports are opened on the router.
  Tailscale encrypts everything between the phone and the PC (WireGuard).
- Nothing works without signing in: a password and a 6-digit authenticator code (auth.py).
  Each phone gets its own revocable session, and the PC says out loud when a new phone signs
  in or someone keeps getting it wrong.
- Requests from the phone run as "remote": anything that controls the PC (typing, clicking,
  windows, the browser, Claude's "work it out") asks Yes/No on the phone first
  (phone.pc_control: ask; "off" refuses them), a few things are never done remotely (voice lock,
  teaching, dictation), and the audit log marks every one of them as remote.
- Voice messages (remote/voice.py) are heard and answered by the PC, like the headset.
- Same page hardening as the HUD: strict CSP, textContent only, Host/Origin checks.
- Replies appear on the phone; nothing is said out loud at home.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import shutil
import socket
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from assistant.core.config import Settings
from assistant.remote.auth import LoginError, PhoneAuth

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
COOKIE = "nova_phone"
_FILES = {"/": ("phone.html", "text/html"), "/phone.js": ("phone.js", "text/javascript"),
          "/phone.css": ("phone.css", "text/css"), "/icon.svg": ("icon.svg", "image/svg+xml"),
          "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
          # Nova's bubble: the same one as the PC window, so the phone looks and moves the same.
          "/orb.js": (Path(__file__).parent.parent / "hud" / "static" / "orb.js", "text/javascript")}
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
# Services the phone's requests may use (the mouse too: clicking asks on the phone first).
# Not the lesson recorder or the voice lock.
SHARED_SERVICES = ("scheduler", "watchers", "memory", "subtitles", "voice", "spotify", "grid")
KEEP = {"you", "text", "tool_started", "tool_finished", "turn_complete", "error", "announcement", "sys"}

# Buttons on the phone: fixed tool calls, nothing free-form.
ACTIONS: dict[str, tuple[str, dict] | str] = {
    "pause": ("media", {"action": "pause"}), "play": ("media", {"action": "play"}),
    "next": ("media", {"action": "next"}), "previous": ("media", {"action": "previous"}),
    "vol_down": ("volume", {"action": "down", "amount": 10}), "vol_up": ("volume", {"action": "up", "amount": 10}),
    "mute": ("volume", {"action": "mute"}), "lock": ("lock_pc", {}),
    "stand_down": "stand_down", "wake": "resume", "stop": "stop",
}


def tailnet_peer(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return any(ip in net for net in TAILNET)


def _tailscale_exe() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    if sys.platform == "win32":
        p = Path(r"C:\Program Files\Tailscale\tailscale.exe")
        return str(p) if p.exists() else None
    return None


def tailscale_info() -> dict[str, Any]:
    """{'installed', 'ip', 'dns'}: the PC's Tailscale IPv4 and MagicDNS name, if it's up."""
    exe = _tailscale_exe()
    info: dict[str, Any] = {"installed": exe is not None, "ip": None, "dns": None}
    flags = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    if exe:
        try:
            out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=5, **flags)
            data = json.loads(out.stdout or "{}")
            me = data.get("Self") or {}
            ips = [a for a in me.get("TailscaleIPs") or [] if "." in a]
            if data.get("BackendState") == "Running" and ips:
                info["ip"] = ips[0]
                info["dns"] = (me.get("DNSName") or "").rstrip(".") or None
        except Exception as e:
            log.debug("tailscale status failed: %s", e)
    if info["ip"] is None:                                  # no CLI: look at the network cards
        try:
            import psutil
            for addrs in psutil.net_if_addrs().values():
                for a in addrs:
                    if a.family == socket.AF_INET and tailnet_peer(a.address):
                        info["ip"] = a.address
                        info["installed"] = True
        except Exception:
            pass
    return info


def device_name(user_agent: str) -> str:
    ua = user_agent or ""
    for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android phone"),
                      ("Windows", "Windows PC"), ("Macintosh", "Mac")):
        if key in ua:
            break
    else:
        name = "Phone"
    for key, browser in (("EdgA", "Edge"), ("Edg/", "Edge"), ("CriOS", "Chrome"), ("Chrome", "Chrome"),
                         ("FxiOS", "Firefox"), ("Firefox", "Firefox"), ("Safari", "Safari")):
        if key in ua:
            return f"{name} · {browser}"
    return name


class PhoneChat:
    """One signed-in phone's conversation. It outlives the connection (phones drop the socket
    when the screen locks), and the last 100 events are replayed on reconnect."""

    def __init__(self, brain, services: dict, client_id: str, speaker=None):
        from assistant.core.session import Session
        self.backlog: deque[dict] = deque(maxlen=100)
        self.sockets: set = set()
        self.session = Session(brain, self.send, client_id=client_id, remote=True)
        self.session.ctx.services.update(services)
        self.speaker = speaker                 # text -> base64 WAV in Nova's voice (or None)
        self.speak_next = False                # the request was spoken: answer out loud
        self._speaking: asyncio.Task | None = None
        self._said: list[str] = []             # what this turn showed, to say out loud

    async def send(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind in KEEP:
            self.backlog.append({**msg, "ts": time.time()})
        if kind == "you":
            self._said = []
        elif kind == "text":
            self._said.append(str(msg.get("text") or ""))
        elif kind in ("turn_complete", "cancelled") and self.speak_next:
            self.speak_next = False
            said = "".join(self._said).strip() or str(msg.get("text") or "").strip()
            if kind == "turn_complete" and self.speaker and said:
                self._speaking = asyncio.ensure_future(self._speak(said))
        for s in list(self.sockets):
            with contextlib.suppress(Exception):
                await s(msg)

    async def _speak(self, text: str) -> None:
        try:
            data = await self.speaker(text)
        except Exception as e:
            log.warning("phone voice reply failed: %s", e)
            return
        if data:
            for s in list(self.sockets):
                with contextlib.suppress(Exception):
                    await s({"type": "audio", "mime": "audio/wav", "data": data})


def create_phone_app(settings: Settings, auth: PhoneAuth, loop, hub, scheduler=None, watchers=None,
                     trust_test_client: bool = False, chats: dict | None = None,
                     names: set[str] | None = None, secure_url: str | None = None,
                     voice_note: str = "") -> FastAPI:
    from assistant.hud.server import status as loop_status, timers as timer_list, vitals

    port = settings.phone.port
    chats = {} if chats is None else chats
    tasks: set[asyncio.Task] = set()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def peer_ok(host: str | None) -> bool:
        return tailnet_peer(host) or (trust_test_client and host == "testclient")

    allowed = {n.lower() for n in (names or set()) if n} | {socket.gethostname().lower()}
    if trust_test_client:
        allowed.add("testserver")

    def host_ok(host: str) -> bool:
        """Only this PC's own names and Tailscale address (blocks DNS rebinding)."""
        name, _, p = host.rpartition(":")
        return p == str(port) and name.strip("[]").lower() in allowed

    def origin_ok(headers) -> bool:
        host = headers.get("host", "")
        return headers.get("origin") in (f"http://{host}", f"https://{host}")

    def spawn(coro) -> None:
        t = asyncio.ensure_future(coro)
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    def notify_pc(text: str, speak: bool = False) -> None:
        hub.publish({"type": "toast", "text": text})
        hub.publish({"type": "remote", "text": text})
        if speak and hasattr(loop, "announce"):
            spawn(loop.announce(text))

    auth.on_lockout = lambda n: notify_pc(
        f"Sir, someone got the phone sign-in wrong {n} times. I've locked it for 15 minutes.", speak=True)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        client = request.client.host if request.client else None
        host = request.headers.get("host", "")
        if not peer_ok(client) or not host_ok(host):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        resp: Response = await call_next(request)
        resp.headers.update({
            "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; "
                                        f"img-src 'self' data:; media-src 'self' blob:; manifest-src 'self'; "
                                        f"connect-src 'self' ws://{host} wss://{host}; "
                                        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store", "X-Frame-Options": "DENY"})
        return resp

    for path, (name, media) in _FILES.items():
        def make(name=name, media=media):
            async def serve() -> FileResponse:
                return FileResponse(STATIC / name, media_type=media)
            return serve
        app.get(path)(make())

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "?"

    @app.get("/api/me")
    async def me(request: Request):
        d = auth.device_for(request.cookies.get(COOKIE))
        if d is None:
            return JSONResponse({"signed_in": False, "enabled": auth.enabled}, status_code=401)
        return {"signed_in": True, "device": d.name, "name": settings.assistant.name}

    @app.post("/api/login")
    async def login(request: Request):
        if not origin_ok(request.headers):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad request"}, status_code=400)
        name = device_name(request.headers.get("user-agent", ""))
        try:
            token = await asyncio.to_thread(auth.login, str(body.get("password", "")), str(body.get("code", "")), name)
        except LoginError as e:
            log.warning("phone sign-in refused (%s, %s): %s", client_ip(request), name, e)
            await asyncio.sleep(0.4)                    # slows guessing a little more
            return JSONResponse({"error": str(e), "retry_after": e.retry_after},
                                status_code=429 if e.retry_after else 401)
        log.warning("phone signed in: %s (%s)", name, client_ip(request))
        notify_pc(f"Sir, a new device just signed in to Nova remotely: {name.replace(' · ', ' on ')}.", speak=True)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, token, max_age=auth.session_s, httponly=True, samesite="strict", path="/")
        return resp

    @app.post("/api/logout")
    async def logout(request: Request):
        if not origin_ok(request.headers):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        d = auth.device_for(request.cookies.get(COOKIE))
        if d is not None:
            auth.revoke(d.id)
            chats.pop(d.id, None)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    async def run_action(name: str, chat: PhoneChat) -> str:
        what = ACTIONS.get(name)
        if what is None:
            return "Unknown button."
        if what == "stand_down":
            await loop.stand_down()
            return "Nova is standing down at home."
        if what == "resume":
            await loop.resume()
            return "Nova is back on duty at home."
        if what == "stop":
            await loop.interrupt(reason="phone")
            chat.session.cancel_turn()
            return "Stopped."
        tool, args = what
        res = await loop.brain.registry.execute(tool, dict(args), chat.session.ctx)
        text = res.content if isinstance(res.content, str) else "Done."
        return text if not res.is_error else f"Couldn't: {text}"

    async def speak_reply(text: str) -> str | None:
        from assistant.remote import voice as phone_voice
        return await phone_voice.speak(loop, settings, text)

    def chat_for(device) -> PhoneChat:
        chat = chats.get(device.id)
        if chat is None:
            services = {k: v for k, v in loop.ctx.services.items() if k in SHARED_SERVICES}
            chat = chats[device.id] = PhoneChat(loop.brain, services, client_id=f"phone:{device.id}",
                                                speaker=speak_reply if settings.phone.speak_replies
                                                and getattr(loop, "tts", None) is not None else None)
        return chat

    @app.post("/api/voice")
    async def voice_message(request: Request):
        """A voice message from the phone: heard by the PC's Whisper, answered like the headset."""
        if not origin_ok(request.headers):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        device = auth.device_for(request.cookies.get(COOKIE))
        if device is None:
            return JSONResponse({"error": "signed out"}, status_code=401)
        data = await request.body()
        from assistant.remote import voice as phone_voice
        if len(data) > phone_voice.MAX_BYTES:
            return JSONResponse({"error": "That message is too long."}, status_code=413)
        chat = chat_for(device)
        try:
            text = await phone_voice.transcribe(loop, data)
        except Exception as e:
            log.warning("phone voice message failed: %s", e)
            return JSONResponse({"error": "I couldn't hear that one. Try again?"}, status_code=500)
        text = " ".join(text.split())[:2000]
        if not text:
            await chat.send({"type": "sys", "text": "I didn't catch that."})
            return {"text": ""}
        chat.speak_next = True
        await chat.send({"type": "you", "text": text, "voice": True})
        hub.publish({"type": "remote", "text": f"From your phone (voice): {text}"})
        chat.session.start_turn(text)
        return {"text": text}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        client = websocket.client.host if websocket.client else None
        if not peer_ok(client) or not host_ok(websocket.headers.get("host", "")) or not origin_ok(websocket.headers):
            await websocket.close(code=4403)
            return
        device = auth.device_for(websocket.cookies.get(COOKIE))
        if device is None:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        chat = chat_for(device)
        lock = asyncio.Lock()

        async def send(msg: dict[str, Any]) -> None:
            async with lock:
                await websocket.send_text(json.dumps(msg, default=str))

        await send({"type": "hello", "name": settings.assistant.name, "user": settings.assistant.address_user_as,
                    "device": device.name, "backlog": list(chat.backlog),
                    "voice": bool(getattr(loop, "stt", None)), "secure_url": secure_url, "voice_note": voice_note})
        chat.sockets.add(send)

        async def pump() -> None:
            n = 0
            while True:
                if auth.device_for(websocket.cookies.get(COOKIE)) is None:   # signed out from the PC
                    await websocket.close(code=4401)
                    return
                st = loop_status(loop, settings)
                await send({"type": "status", "state": st["state"], "standby": st["standby"],
                            "muted": st["muted"]})
                await send(timer_list(scheduler, settings, watchers))
                if n % 5 == 0:
                    await send(await asyncio.to_thread(vitals))
                n += 1
                await asyncio.sleep(1.0)

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
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("type")
                if kind == "text" and isinstance(msg.get("text"), str) and msg["text"].strip():
                    text = " ".join(msg["text"].split())[:2000]
                    await chat.send({"type": "you", "text": text})
                    hub.publish({"type": "remote", "text": f"From your phone: {text}"})
                    chat.session.start_turn(text)
                elif kind == "confirm" and isinstance(msg.get("id"), str):
                    chat.session.resolve_confirm(msg["id"], msg.get("approved") is True)
                elif kind == "cancel":
                    chat.session.cancel_turn()
                elif kind == "action" and msg.get("name") in ACTIONS:
                    async def act(name=msg["name"]) -> None:
                        try:
                            text = await run_action(name, chat)
                        except Exception as e:
                            text = f"Couldn't: {e}"
                        hub.publish({"type": "remote", "text": f"Phone button: {name.replace('_', ' ')}"})
                        await chat.send({"type": "sys", "text": text})
                    spawn(act())
                elif kind == "cancel_timer" and scheduler is not None and isinstance(msg.get("id"), str):
                    if any(r.id == msg["id"] for r in scheduler.upcoming()):
                        scheduler.cancel(msg["id"])
                elif kind == "cancel_watch" and watchers is not None and isinstance(msg.get("id"), str):
                    watchers.cancel(msg["id"])
        except WebSocketDisconnect:
            pass
        finally:
            chat.sockets.discard(send)
            pumper.cancel()
            await asyncio.gather(pumper, return_exceptions=True)

    app.state.chats = chats
    return app


CERT_DIR_NAME = "phone-cert"


def tailscale_cert(dns: str, folder: Path) -> tuple[str, str]:
    """An HTTPS certificate for this PC's Tailscale name (phones only allow the mic on https).
    Needs HTTPS turned on once for the tailnet (Tailscale admin: DNS -> HTTPS Certificates)."""
    exe = _tailscale_exe()
    if not exe:
        raise RuntimeError("Tailscale isn't installed")
    folder.mkdir(parents=True, exist_ok=True)
    cert, key = folder / "cert.pem", folder / "key.pem"
    flags = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    out = subprocess.run([exe, "cert", "--cert-file", str(cert), "--key-file", str(key), dns],
                         capture_output=True, text=True, timeout=90, **flags)
    if out.returncode != 0 or not cert.exists() or not key.exists():
        why = (out.stderr or out.stdout or "").strip().splitlines()
        raise RuntimeError(why[-1][:200] if why else f"tailscale cert failed ({out.returncode})")
    return str(cert), str(key)


class PhoneService:
    """Starts and stops the phone server as phone access is switched on/off and as Tailscale
    comes and goes (it's often not up yet when Nova starts at sign-in)."""

    CHECK_EVERY_S = 10.0

    def __init__(self, settings: Settings, loop, hub, scheduler=None, watchers=None, auth: PhoneAuth | None = None):
        self.settings, self.loop, self.hub = settings, loop, hub
        self.scheduler, self.watchers = scheduler, watchers
        self.auth = auth or PhoneAuth(settings.phone.session_days)
        self.chats: dict = {}
        self.ts: dict[str, Any] = {"installed": False, "ip": None, "dns": None}
        self.bound_ip: str | None = None
        self.error = ""
        self._server = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self.secure = False                    # serving https (Tailscale certificate)
        self.voice_note = ""                   # why voice messages can't work yet, in plain words

    @property
    def running(self) -> bool:
        return self._server is not None

    def addresses(self) -> list[str]:
        if not self.bound_ip:
            return []
        port = self.settings.phone.port
        if self.secure:                         # the certificate is for the name, not the number
            return [f"https://{self.ts['dns']}:{port}"]
        out = [f"http://{self.ts['dns']}:{port}"] if self.ts.get("dns") else []
        return out + [f"http://{self.bound_ip}:{port}"]

    def info(self) -> dict[str, Any]:
        return {"type": "phone", "configured": self.auth.configured, "enabled": self.auth.enabled,
                "running": self.running, "tailscale": bool(self.ts.get("installed")),
                "tailscale_up": bool(self.ts.get("ip")), "addresses": self.addresses(),
                "devices": self.auth.devices(), "locked_for": self.auth.locked_for(), "error": self.error,
                "secure": self.secure, "voice_note": self.voice_note}

    async def refresh_tailscale(self) -> None:
        """For the Phone page: is Tailscale on this PC (even before phone access is set up)?"""
        self.ts = await asyncio.to_thread(tailscale_info)

    def poke(self) -> None:
        self._wake.set()

    async def _forward(self) -> None:
        """Reminders and watch alerts also reach signed-in phones."""
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.hub.clients.add(q)
        try:
            while True:
                ev = await q.get()
                if ev.get("type") == "announcement":
                    for chat in list(self.chats.values()):
                        await chat.send({"type": "announcement", "text": ev.get("text", "")})
        finally:
            self.hub.clients.discard(q)

    async def run(self) -> None:
        forward = asyncio.create_task(self._forward())
        try:
            await self._run()
        finally:
            forward.cancel()

    async def _run(self) -> None:
        while True:
            try:
                await self._reconcile()
            except Exception as e:
                log.warning("phone access: %s", e)
                self.error = str(e)
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.CHECK_EVERY_S)

    async def _reconcile(self) -> None:
        want = self.auth.enabled and self.auth.configured
        if want:
            self.ts = await asyncio.to_thread(tailscale_info)
        ip = self.ts.get("ip") if want else None
        if self.running and ip != self.bound_ip:
            await self.stop()
        if ip and not self.running:
            await self._start(ip)

    async def _start(self, ip: str) -> None:
        from assistant.hud.server import _QuietServer
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((ip, self.settings.phone.port))       # the Tailscale address only
            sock.listen(16)
            sock.setblocking(False)
        except OSError as e:
            sock.close()
            self.error = f"Couldn't listen on {ip}:{self.settings.phone.port} ({e})"
            return
        tls, self.secure, self.voice_note = None, False, ""
        dns = self.ts.get("dns")
        if self.settings.phone.https and dns:
            from assistant.core.config import ROOT
            try:
                tls = await asyncio.to_thread(tailscale_cert, dns, ROOT / "data" / CERT_DIR_NAME)
                self.secure = True
            except Exception as e:
                log.warning("phone access: no HTTPS certificate (%s); voice messages need it", e)
                self.voice_note = ("Voice messages need HTTPS: in the Tailscale admin page, open DNS and "
                                   "turn on MagicDNS and HTTPS Certificates, then restart Nova.")
        elif not dns:
            self.voice_note = "Voice messages need Tailscale's MagicDNS name for this PC (turn on MagicDNS)."
        secure_url = f"https://{dns}:{self.settings.phone.port}" if self.secure else None
        app = create_phone_app(self.settings, self.auth, self.loop, self.hub, self.scheduler, self.watchers,
                               chats=self.chats, names={ip, dns or ""}, secure_url=secure_url,
                               voice_note=self.voice_note)
        self._server = _QuietServer(app, sock, ssl=tls)
        self._task = asyncio.create_task(self._server.serve())
        self.bound_ip, self.error = ip, ""
        log.info("phone access on %s", ", ".join(self.addresses()))

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._task, 3)
        self._server = self._task = None
        self.bound_ip = None
        for chat in self.chats.values():
            chat.session.cancel_turn()
        self.chats.clear()
