from fastapi.testclient import TestClient

from tests.fakes import FakeClient, text_msg, tool_msg
from assistant.brain.llm import Brain
from assistant.core.server import create_app
from assistant.tools.registry import Risk


def recv_until(ws, kind):
    out = []
    while True:
        msg = ws.receive_json()
        out.append(msg)
        if msg["type"] == kind:
            return out


def test_health_and_chat(settings, registry):
    brain = Brain(settings, registry, FakeClient([text_msg("Hello, sir.")]))
    client = TestClient(create_app(settings, brain))
    assert client.get("/health").json()["ok"]
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "user_text", "text": "hi"})
        msgs = recv_until(ws, "turn_complete")
        assert msgs[-1]["text"] == "Hello, sir."


def test_confirmation_round_trip(settings, registry):
    ran = []

    @registry.tool("lock_pc", "lock", risk=Risk.CONFIRM)
    def lock(args, ctx):
        ran.append(True)
        return "locked"

    brain = Brain(settings, registry, FakeClient([tool_msg("lock_pc", {}), text_msg("Locked.")]))
    client = TestClient(create_app(settings, brain))
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "user_text", "text": "lock it"})
        req = recv_until(ws, "confirm_request")[-1]
        assert req["tool"] == "lock_pc"
        ws.send_json({"type": "confirm_response", "id": req["id"], "approved": True})
        recv_until(ws, "turn_complete")
    assert ran == [True]
    assert client.get("/audit").json()[-1]["confirmed"] is True
