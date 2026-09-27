import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from assistant.brain.llm import Brain
from assistant.core.server import create_app
from assistant.tools.registry import Risk
from tests.fakes import FakeClient, text_msg, tool_msg

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def app_client(settings, brain):
    settings.server.allowed_hosts = ["testserver", "127.0.0.1", "localhost"]
    return TestClient(create_app(settings, brain, token=TOKEN, trust_test_client=True))


def recv_until(ws, kind):
    out = []
    while True:
        msg = ws.receive_json()
        out.append(msg)
        if msg["type"] == kind:
            return out


def test_health_and_chat(settings, registry):
    client = app_client(settings, Brain(settings, registry, FakeClient([text_msg("Hello, sir.")])))
    assert client.get("/health").json()["ok"]
    with client.websocket_connect("/ws", headers=AUTH) as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "user_text", "text": "hi"})
        assert recv_until(ws, "turn_complete")[-1]["text"] == "Hello, sir."


def test_auth_by_first_message(settings, registry):
    client = app_client(settings, Brain(settings, registry, FakeClient([])))
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": TOKEN})
        assert ws.receive_json()["type"] == "hello"


def test_rejects_missing_or_wrong_token(settings, registry):
    client = app_client(settings, Brain(settings, registry, FakeClient([])))
    for first in ({"type": "user_text", "text": "hi"}, {"type": "auth", "token": "wrong"}):
        with client.websocket_connect("/ws") as ws:
            ws.send_json(first)
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_json()
            assert e.value.code == 4401
    assert client.get("/audit").status_code == 401
    assert client.get("/audit", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/audit", headers=AUTH).status_code == 200


def test_rejects_browser_origin_and_rebinding(settings, registry):
    client = app_client(settings, Brain(settings, registry, FakeClient([])))
    # A web page you visit: browsers always send Origin.
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws", headers={**AUTH, "Origin": "https://evil.example"}) as ws:
            ws.receive_json()
    assert e.value.code == 4403
    # DNS rebinding: evil.example resolving to 127.0.0.1 still sends Host: evil.example.
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/health", headers={"Origin": "https://evil.example"}).status_code == 403
    # No API docs exposed.
    assert client.get("/docs").status_code == 404


def test_non_loopback_peer_rejected(settings, registry):
    settings.server.allowed_hosts = ["testserver"]
    client = TestClient(create_app(settings, Brain(settings, registry, FakeClient([])), token=TOKEN))
    assert client.get("/health").status_code == 403  # "testclient" peer is not loopback


def test_confirmation_round_trip(settings, registry):
    ran = []

    @registry.tool("lock_pc", "lock", risk=Risk.CONFIRM)
    def lock(args, ctx):
        ran.append(True)
        return "locked"

    client = app_client(settings, Brain(settings, registry,
                                        FakeClient([tool_msg("lock_pc", {}), text_msg("Locked.")])))
    with client.websocket_connect("/ws", headers=AUTH) as ws:
        ws.receive_json()
        ws.send_json({"type": "user_text", "text": "lock it"})
        req = recv_until(ws, "confirm_request")[-1]
        assert req["tool"] == "lock_pc"
        ws.send_json({"type": "confirm_response", "id": req["id"], "approved": True})
        recv_until(ws, "turn_complete")
    assert ran == [True]
    assert client.get("/audit", headers=AUTH).json()[-1]["confirmed"] is True
