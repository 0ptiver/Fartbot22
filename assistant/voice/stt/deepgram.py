"""Cloud streaming STT with Deepgram (optional). Audio streams while you talk,
so the final transcript arrives ~100-200 ms after end-of-speech."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode

import numpy as np

from assistant.core.config import DeepgramConfig
from assistant.core.secrets import get_secret
from assistant.voice.stt.base import STTProvider, STTSession, Transcript

URL = "wss://api.deepgram.com/v1/listen"


class DeepgramSTT(STTProvider):
    name = "deepgram"

    def __init__(self, cfg: DeepgramConfig):
        self.cfg = cfg

    async def load(self) -> None:
        if not get_secret("DEEPGRAM_API_KEY"):
            raise RuntimeError("DEEPGRAM_API_KEY is not set")

    def session(self) -> STTSession:
        return DeepgramSession(self.cfg)


class DeepgramSession(STTSession):
    def __init__(self, cfg: DeepgramConfig):
        self.cfg = cfg
        self._ws = None
        self._connecting: asyncio.Task | None = None
        self._finals: list[str] = []
        self._finalized = asyncio.Event()
        self._reader: asyncio.Task | None = None

    async def _connect(self):
        import websockets

        params = urlencode({
            "model": self.cfg.model, "encoding": "linear16", "sample_rate": 16000,
            "channels": 1, "punctuate": "true", "smart_format": "true",
            "interim_results": "true", "endpointing": "false",
        })
        ws = await websockets.connect(
            f"{URL}?{params}",
            additional_headers={"Authorization": f"Token {get_secret('DEEPGRAM_API_KEY')}"},
        )
        self._reader = asyncio.create_task(self._read(ws))
        return ws

    async def _read(self, ws) -> None:
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "Results":
                    continue
                alt = msg["channel"]["alternatives"][0]
                if msg.get("is_final") and alt.get("transcript"):
                    self._finals.append(alt["transcript"])
                if msg.get("from_finalize"):
                    self._finalized.set()
        finally:
            self._finalized.set()

    async def _get_ws(self):
        if self._connecting is None:
            self._connecting = asyncio.create_task(self._connect())
        return await self._connecting

    async def feed(self, audio: np.ndarray) -> None:
        ws = await self._get_ws()
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        await ws.send(pcm)

    async def finish(self) -> Transcript:
        ws = await self._get_ws()
        await ws.send(json.dumps({"type": "Finalize"}))
        try:
            await asyncio.wait_for(self._finalized.wait(), 3)
        except asyncio.TimeoutError:
            pass  # use whatever finals arrived
        finally:
            try:
                await ws.send(json.dumps({"type": "CloseStream"}))
                await ws.close()
            except Exception:
                pass
        return Transcript(" ".join(self._finals).strip(), "en")

    async def abort(self) -> None:
        if self._connecting:
            ws = await self._connecting
            await ws.close()
