"""FastAPI server: WebSocket for clients, small HTTP API for health and audit."""

from __future__ import annotations

import asyncio
import ipaddress
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from vesper.brain.llm import Brain
from vesper.core.config import Settings, load_settings
from vesper.core.session import Session
from vesper.tools import build_registry


def _is_loopback(host: str | None) -> bool:
    if host in (None, "testclient", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_app(settings: Settings | None = None, brain: Brain | None = None) -> FastAPI:
    settings = settings or load_settings()
    if brain is None:
        brain = Brain(settings, build_registry(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(title="Vesper", lifespan=lifespan)
    app.state.brain = brain

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "name": settings.assistant.name,
                "model": settings.brain.chat_model, "tools": brain.registry.names()}

    @app.get("/audit")
    async def audit(n: int = 50) -> list[dict[str, Any]]:
        return await asyncio.to_thread(brain.registry.audit.tail, min(max(n, 1), 500))

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        # Phase 1 is local-only. Remote clients get auth + a stricter policy in Phase 6.
        client_host = websocket.client.host if websocket.client else None
        if not _is_loopback(client_host):
            await websocket.close(code=4403)
            return
        await websocket.accept()
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
