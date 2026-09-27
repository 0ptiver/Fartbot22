"""FastAPI server: WebSocket for clients, small HTTP API for health and audit.

Security (local-only phase):
- binds to 127.0.0.1 and refuses non-loopback peers;
- every client must present the local client token (Windows Credential Manager);
- rejects unexpected Host headers (DNS rebinding) and any browser Origin not
  explicitly allowed, so a web page you visit cannot drive the assistant.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect

from assistant.brain.llm import Brain
from assistant.core.config import Settings, load_settings
from assistant.core.secrets import local_client_token
from assistant.core.session import Session
from assistant.tools import build_registry

AUTH_TIMEOUT_S = 5


def _is_loopback(host: str | None) -> bool:
    if host is None:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_only(host_header: str | None) -> str:
    if not host_header:
        return ""
    if host_header.startswith("["):  # [::1]:8765
        return host_header[1:host_header.find("]")]
    return host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header


def _token_ok(presented: str | None, expected: str) -> bool:
    return bool(presented) and hmac.compare_digest(presented.encode(), expected.encode())


def _bearer(value: str | None) -> str | None:
    if value and value.lower().startswith("bearer "):
        return value[7:].strip()
    return None


def create_app(settings: Settings | None = None, brain: Brain | None = None,
               token: str | None = None, trust_test_client: bool = False) -> FastAPI:
    settings = settings or load_settings()
    if brain is None:
        brain = Brain(settings, build_registry(settings))
    token = token or local_client_token()
    allowed_hosts = {h.lower() for h in settings.server.allowed_hosts}
    allowed_origins = set(settings.server.allowed_origins)

    def peer_ok(host: str | None) -> bool:
        return _is_loopback(host) or (trust_test_client and host == "testclient")

    def headers_ok(host_header: str | None, origin: str | None) -> bool:
        if _host_only(host_header).lower() not in allowed_hosts:
            return False
        return origin is None or origin in allowed_origins

    app = FastAPI(title="Assistant", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.brain = brain

    @app.middleware("http")
    async def guard(request: Request, call_next):
        client = request.client.host if request.client else None
        if not peer_ok(client) or not headers_ok(request.headers.get("host"),
                                                 request.headers.get("origin")):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/audit")
    async def audit(n: int = 50, authorization: str | None = Header(None)) -> list[dict[str, Any]]:
        if not _token_ok(_bearer(authorization), token):
            raise HTTPException(401, "unauthorized")
        return await asyncio.to_thread(brain.registry.audit.tail, min(max(n, 1), 500))

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        client_host = websocket.client.host if websocket.client else None
        if not peer_ok(client_host) or not headers_ok(websocket.headers.get("host"),
                                                      websocket.headers.get("origin")):
            await websocket.close(code=4403)
            return
        await websocket.accept()

        # Auth: Bearer header (CLI), or a first {"type": "auth"} message (browser clients later).
        if not _token_ok(_bearer(websocket.headers.get("authorization")), token):
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
                await websocket.send_json(msg)

        session = Session(brain, send, client_id=f"ws:{client_host}")
        await send({"type": "hello", "name": settings.assistant.name})
        try:
            while True:
                msg = await websocket.receive_json()
                if isinstance(msg, dict):
                    await session.handle(msg)
        except WebSocketDisconnect:
            pass
        finally:
            session.cancel_turn()

    return app
