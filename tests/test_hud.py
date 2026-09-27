"""The HUD: security checks, buttons, event fan-out, and the voice-loop hooks it drives."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from assistant.core.scheduler import Scheduler
from assistant.hud import server as hud
from tests.fakes import text_msg, tool_msg
from tests.test_barge import make
from tests.test_safety_voice import add_sleep_tool
from tests.voice_helpers import FakeTTS

KEY = "k" * 43
HOST = {"host": "127.0.0.1:8766"}
ORIGIN = {**HOST, "origin": "http://127.0.0.1:8766"}


class FakeLoop:
    def __init__(self):
        self.calls = []
        self.state, self.mic_level, self.standby, self.mic_muted = "idle", 0.05, False, False
        self._confirm, self.confirm_text = None, ""

    async def submit_text(self, text):
        self.calls.append(("text", text))

    async def stand_down(self):
        self.calls.append(("stand_down",))

    async def resume(self):
        self.calls.append(("resume",))

    async def interrupt(self, reason=""):
        self.calls.append(("stop", reason))

    def answer_confirm(self, approved):
        self.calls.append(("confirm", approved))
        return True


@pytest.fixture
def rig(settings, tmp_path):
    fake, hub = FakeLoop(), hud.Hub()
    sched = Scheduler(tmp_path / "rem.json", settings.assistant.timezone)
    app = hud.create_hud_app(settings, fake, hub, KEY, sched, trust_test_client=True)
    return TestClient(app), fake, hub, sched


def connect(client, key=KEY, headers=ORIGIN):
    ws = client.websocket_connect("/ws", headers=headers).__enter__()
    ws.send_json({"type": "auth", "token": key})
    return ws


def recv(ws, kind, limit=50):
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == kind:
            return msg
    raise AssertionError(f"no {kind}")


def test_page_is_served_with_strict_headers(rig):
    client = rig[0]
    r = client.get("/", headers=HOST)
    assert r.status_code == 200 and "hud.js" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert client.get("/hud.js", headers=HOST).headers["content-type"].startswith("text/javascript")
    assert client.get("/", headers={"host": "evil.example:8766"}).status_code == 403   # DNS rebinding
    assert client.get("/secret.txt", headers=HOST).status_code == 404


def test_websocket_needs_own_origin_and_key(rig):
    client = rig[0]
    for headers in ({**HOST, "origin": "https://evil.example"}, HOST, {"host": "evil:8766", "origin": ORIGIN["origin"]}):
        with pytest.raises(WebSocketDisconnect) as e:
            with client.websocket_connect("/ws", headers=headers) as ws:
                ws.receive_json()
        assert e.value.code == 4403
    with pytest.raises(WebSocketDisconnect) as e:
        ws = connect(client, key="wrong")
        ws.receive_json()
    assert e.value.code == 4401


def test_hello_replays_history(rig, settings):
    client, fake, hub, _ = rig
    hub.publish({"type": "transcript", "text": "what time is it"})
    hub.publish({"type": "status", "state": "idle"})            # not kept
    ws = connect(client)
    hello = recv(ws, "hello")
    assert [e["type"] for e in hello["backlog"]] == ["transcript"]
    assert hello["name"] == "Nova" and hello["confirm_timeout_s"] == settings.voice.confirm_timeout_s
    st = recv(ws, "status")
    assert st["state"] == "idle" and 0 <= st["level"] <= 1
    ws.close()


def test_buttons_drive_the_voice_loop(rig, settings):
    client, fake, hub, sched = rig
    t = sched.add("timer", time.time() + 300, "pasta")
    ws = connect(client)
    recv(ws, "hello")
    for msg in [{"type": "text", "text": "what's the weather"}, {"type": "confirm", "approved": True},
                {"type": "confirm", "approved": "yes"}, {"type": "stand_down"}, {"type": "resume"},
                {"type": "stop"}, {"type": "mute", "on": True}, {"type": "mode", "mode": "open_mic"},
                {"type": "mode", "mode": "ptt"}, {"type": "made_up"}]:
        ws.send_json(msg)
    ws.send_json({"type": "cancel_timer", "id": t.id})
    for _ in range(10):                                           # the list is re-sent without it
        if recv(ws, "timers")["items"] == []:
            break
    else:
        raise AssertionError("timer still listed")
    ws.send_text("not json")
    ws.send_json({"type": "routine", "name": "nope"})            # unknown routine: nothing
    time.sleep(0.2)
    ws.close()
    assert ("text", "what's the weather") in fake.calls
    assert ("confirm", True) in fake.calls and ("confirm", False) in fake.calls   # only literal true is yes
    assert ("stand_down",) in fake.calls and ("resume",) in fake.calls
    assert any(c[0] == "stop" for c in fake.calls)
    assert fake.mic_muted is True and settings.voice.mode == "open_mic"        # ptt refused
    assert sched.upcoming() == []


def test_routine_button_runs_its_phrase(rig, settings):
    from assistant.core.config import RoutineConfig
    settings.routines = {"gaming_mode": RoutineConfig(phrases=["gaming mode"], steps=[])}
    client, fake, *_ = rig
    ws = connect(client)
    assert recv(ws, "hello")["routines"][0]["label"] == "Gaming mode"
    ws.send_json({"type": "routine", "name": "gaming_mode"})
    time.sleep(0.2)
    ws.close()
    assert ("text", "gaming mode") in fake.calls


def test_hub_never_blocks():
    hub = hud.Hub(backlog=3)
    q = asyncio.Queue(maxsize=1)
    hub.clients.add(q)
    for i in range(5):
        hub.publish({"type": "text", "text": str(i)})
    assert q.qsize() == 1 and [e["text"] for e in hub.backlog] == ["2", "3", "4"]


def test_window_uses_its_own_profile():
    cmd = hud.window_command("msedge.exe", "http://127.0.0.1:8766/#k=abc")
    assert cmd[1] == "--app=http://127.0.0.1:8766/#k=abc"
    assert any(a.startswith("--user-data-dir=") for a in cmd) and "--disable-extensions" in cmd


async def test_start_hud_serves_on_loopback(settings, tmp_path, monkeypatch):
    import socket
    import websockets
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    settings.hud.port = s.getsockname()[1]
    s.close()
    settings.hud.open_window = False
    monkeypatch.setattr(hud, "URL_FILE", tmp_path / "hud-url.txt")
    handle = await hud.start_hud(settings, FakeLoop(), hud.Hub())
    try:
        assert handle is not None and (tmp_path / "hud-url.txt").read_text() == handle.url
        key = handle.url.split("#k=")[1]
        assert await hud.start_hud(settings, FakeLoop(), hud.Hub()) is None   # port taken: no crash
        port = settings.hud.port
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws", origin=f"http://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps({"type": "auth", "token": key}))
            assert json.loads(await ws.recv())["type"] == "hello"
    finally:
        await handle.close()
    assert not (tmp_path / "hud-url.txt").exists()


# --- voice-loop hooks -------------------------------------------------------------------------
async def test_typed_request_gets_a_spoken_reply(settings, registry):
    loop, events = make(settings, registry, [text_msg("It is noon, sir.")],
                        [("quiet", 5), ("until", lambda l: l.turns_done >= 1 and not l.busy)], tts=FakeTTS())

    async def type_it():
        await asyncio.sleep(0.02)
        await loop.submit_text("  what   time is it ")
    typer = asyncio.create_task(type_it())
    await loop.run()
    await typer
    assert any(e["type"] == "transcript" and e.get("typed") and e["text"] == "what time is it" for e in events)
    assert loop.tts.spoken[-1] == "It is noon, sir."


async def test_confirm_buttons(settings, registry):
    ran = []
    add_sleep_tool(registry, ran)
    loop, events = make(settings, registry, [tool_msg("sleep_pc", {}), text_msg("Goodnight, sir.")], [
        ("say", "Nova, put the PC to sleep", 10), ("quiet", 20),
        ("until", lambda l: l.state == "confirm" and "Shall I" in l._last_said),
        ("until", lambda l: l.confirm_text == "put the PC to sleep" and l.answer_confirm(True)),
        ("quiet", 20)], tts=FakeTTS())
    await loop.run()
    assert ran == [True] and loop.answer_confirm(True) is False     # nothing pending any more


async def test_mute_ignores_speech(settings, registry):
    loop, events = make(settings, registry, [text_msg("Noon.")], [("say", "Nova, what time is it", 10), ("quiet", 20)])
    loop.mic_muted = True
    assert loop.state == "muted"
    await loop.run(max_turns=None)
    assert loop.brain.client.messages.calls == [] and loop.mic_level == 0.0


async def test_stand_down_button_and_typed_wake(settings, registry):
    loop, events = make(settings, registry, [text_msg("Noon, sir.")], [("quiet", 5)], tts=FakeTTS())
    await loop.stand_down()
    assert loop.state == "standby"
    await loop.submit_text("what time is it")                   # ignored while standing down
    await loop.submit_text("wake up")                           # typed: no name needed
    assert not loop.standby
    assert "Standing down, sir." in loop.tts.spoken and "At your service, sir." in loop.tts.spoken
    assert loop.brain.client.messages.calls == []
