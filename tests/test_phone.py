"""Phone access: sign-in (password + authenticator code), lockout, sessions, the tailnet-only
guards, and phone requests running as "remote" (so risky tools are refused)."""

import asyncio
import base64
import json
import socket
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from assistant.brain.llm import TextDelta, ToolFinished, ToolStarted, TurnComplete
from assistant.hud import server as hud
from assistant.remote import auth as A
from assistant.remote import server as R
from tests.test_hud import KEY, FakeLoop, connect, recv

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()
HOST = {"host": "testserver:8767"}
ORIGIN = {**HOST, "origin": "http://testserver:8767"}
PW = "correct horse battery"


# --- TOTP + password -------------------------------------------------------------------------
def test_totp_matches_the_rfc_6238_test_vectors():
    assert A.totp(RFC_SECRET, 59 // 30) == "287082"
    assert A.totp(RFC_SECRET, 1111111109 // 30) == "081804"
    assert A.totp(RFC_SECRET, 2000000000 // 30) == "279037"


def test_codes_allow_a_little_clock_drift_but_never_twice():
    now = 1_700_000_000
    step = now // 30
    assert A.totp_step(RFC_SECRET, A.totp(RFC_SECRET, step), now) == step
    assert A.totp_step(RFC_SECRET, A.totp(RFC_SECRET, step - 1), now) == step - 1      # 30 s late: fine
    assert A.totp_step(RFC_SECRET, A.totp(RFC_SECRET, step - 3), now) is None          # 90 s: no
    assert A.totp_step(RFC_SECRET, A.totp(RFC_SECRET, step), now, after=step) is None   # replay
    assert A.totp_step(RFC_SECRET, "12345", now) is None
    code = A.totp(RFC_SECRET, step)
    assert A.totp_step(RFC_SECRET, f"{code[:3]} {code[3:]}", now) == step              # "123 456" typed


def test_password_is_only_kept_as_a_hash():
    rec = A.hash_password(PW)
    assert PW not in json.dumps(rec)
    assert A.check_password(PW, rec) and not A.check_password(PW + "!", rec)
    assert not A.check_password(PW, {"salt": "zz"})


# --- setup + sign-in -------------------------------------------------------------------------
def set_up(auth: A.PhoneAuth) -> str:
    """Set up now. The setup code is spent, so sign-ins in these tests happen a minute later."""
    auth.begin_setup(PW, "Nova on PC")
    secret = auth._pending
    assert auth.finish_setup(A.totp(secret, int(time.time() // 30)))
    return secret


def test_setup_needs_a_long_password_and_a_working_code():
    auth = A.PhoneAuth()
    with pytest.raises(ValueError):
        auth.begin_setup("short", "x")
    shown = auth.begin_setup(PW, "Nova on PC")
    assert shown["qr"].startswith("data:image/svg+xml") and len(shown["secret"].replace(" ", "")) == 32
    secret = auth._pending
    right = A.totp(secret, int(time.time() // 30))
    assert not auth.finish_setup(str((int(right) + 500_000) % 1_000_000).zfill(6))
    assert not auth.configured
    assert auth.finish_setup(A.totp(secret, int(time.time() // 30)))
    assert auth.configured and auth.enabled
    saved = A.CRED_FILE.read_text()
    assert PW not in saved and secret in saved
    assert json.loads(A.STATE_FILE.read_text())["enabled"] is True


def test_sign_in_needs_both_the_password_and_a_fresh_code():
    auth = A.PhoneAuth()
    now = time.time() + 60
    secret = set_up(auth)
    code = A.totp(secret, int(now // 30))
    with pytest.raises(A.LoginError):
        auth.login("wrong password!!", code, "iPhone", now)
    with pytest.raises(A.LoginError):
        auth.login(PW, "000000" if code != "000000" else "111111", "iPhone", now)
    token = auth.login(PW, code, "iPhone", now)
    assert auth.device_for(token).name == "iPhone"
    with pytest.raises(A.LoginError):                        # the same code can't be used again
        auth.login(PW, code, "Someone else", now)
    assert token not in A.STATE_FILE.read_text()             # only a hash of it is stored


def test_five_wrong_tries_lock_sign_in_and_tell_the_pc():
    auth = A.PhoneAuth()
    now = time.time() + 60
    secret = set_up(auth)
    told = []
    auth.on_lockout = told.append
    for i in range(A.MAX_FAILS - 1):
        with pytest.raises(A.LoginError) as e:
            auth.login("guess number %d" % i, "123456", "x", now + i)
        assert e.value.retry_after == 0
    with pytest.raises(A.LoginError) as e:
        auth.login("last guess", "123456", "x", now + 5)
    assert e.value.retry_after == A.LOCK_S and told == [A.MAX_FAILS]
    later = now + 60
    with pytest.raises(A.LoginError):                       # even the right answer, while locked
        auth.login(PW, A.totp(secret, int(later // 30)), "x", later)
    after = now + A.LOCK_S + 30
    assert auth.login(PW, A.totp(secret, int(after // 30)), "x", after)


def test_devices_expire_and_can_be_signed_out():
    auth = A.PhoneAuth(session_days=1)
    now = time.time() + 60
    secret = set_up(auth)
    t1 = auth.login(PW, A.totp(secret, int(now // 30)), "iPhone", now)
    t2 = auth.login(PW, A.totp(secret, int(now // 30) + 1), "iPad", now + 30)
    assert [d["name"] for d in auth.devices()] == ["iPhone", "iPad"]
    assert auth.device_for(t1, now + 2 * 86400) is None      # expired
    auth.revoke(auth.device_for(t2).id)
    assert auth.device_for(t2) is None and auth.device_for(t1) is not None
    auth.revoke()
    assert auth.device_for(t1) is None
    t3 = auth.login(PW, A.totp(secret, int(now // 30) + 2), "iPhone", now + 60)
    auth.set_enabled(False)                                  # switched off on the PC: nobody gets in
    assert auth.device_for(t3) is None


def test_a_new_password_signs_every_phone_out():
    auth = A.PhoneAuth()
    now = time.time() + 60
    secret = set_up(auth)
    token = auth.login(PW, A.totp(secret, int(now // 30)), "iPhone", now)
    set_up(auth)
    assert auth.device_for(token) is None


def test_reset_forgets_everything():
    auth = A.PhoneAuth()
    set_up(auth)
    auth.reset()
    assert not auth.configured and not auth.enabled and not A.CRED_FILE.exists()
    assert A.PhoneAuth().configured is False


# --- the phone server ------------------------------------------------------------------------
class FakeBrain:
    def __init__(self, settings, registry):
        self.settings, self.registry = settings, registry
        self.seen = []

    async def run_turn(self, conv, text, ctx, extra_context=None):
        self.seen.append((text, ctx))
        if text.startswith("type"):
            res = await self.registry.execute("type_text", {"text": "hello"}, ctx)
            yield ToolStarted("t1", "type_text", {})
            yield ToolFinished("t1", "type_text", res.is_error, str(res.content), 5)
        yield TextDelta(f"You said {text}.")
        yield TurnComplete("", {}, {}, "end_turn")


class PhoneLoop(FakeLoop):
    def __init__(self, brain):
        super().__init__()
        self.brain = brain
        self.dictation = False
        self.announced = []
        self.ctx = type("Ctx", (), {"services": {"grid": "GRID", "teacher": "TEACHER", "scheduler": None,
                                                 "memory": "MEM"}})()

    async def announce(self, text):
        self.announced.append(text)


@pytest.fixture
def phone_rig(settings, registry):
    brain = FakeBrain(settings, registry)
    loop = PhoneLoop(brain)
    hub = hud.Hub()
    auth = A.PhoneAuth()
    secret = set_up(auth)
    app = R.create_phone_app(settings, auth, loop, hub, trust_test_client=True)
    return TestClient(app), auth, secret, loop, brain, hub


def sign_in(client, secret, step_offset=1):
    code = A.totp(secret, int(time.time() // 30) + step_offset)
    return client.post("/api/login", json={"password": PW, "code": code},
                       headers={**ORIGIN, "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari/604.1"})


def test_only_tailnet_devices_are_answered():
    assert R.tailnet_peer("100.101.102.103") and R.tailnet_peer("fd7a:115c:a1e0::1")
    assert not R.tailnet_peer("192.168.1.20") and not R.tailnet_peer("8.8.8.8") and not R.tailnet_peer(None)


def test_strangers_and_odd_hosts_get_nothing(settings, phone_rig):
    client, auth, *_ = phone_rig
    assert client.get("/", headers=HOST).status_code == 200
    assert client.get("/", headers={"host": "evil.example:8767"}).status_code == 403
    assert client.get("/", headers={"host": "testserver:9999"}).status_code == 403
    assert client.get("/", headers={"host": "someone-else.tail9.ts.net:8767"}).status_code == 403
    outsider = TestClient(R.create_phone_app(settings, auth, FakeLoop(), hud.Hub()))   # peer isn't on the tailnet
    assert outsider.get("/", headers=HOST).status_code == 403
    page = client.get("/", headers=HOST)
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert client.get("/api/me", headers=HOST).status_code == 401


def test_sign_in_sets_a_private_cookie_and_tells_the_pc(phone_rig):
    client, auth, secret, loop, brain, hub = phone_rig
    assert client.post("/api/login", json={"password": PW, "code": "1"}, headers=HOST).status_code == 403  # no Origin
    bad = client.post("/api/login", json={"password": "nope nope nope", "code": "123456"}, headers=ORIGIN)
    assert bad.status_code == 401 and "isn't right" in bad.json()["error"]
    r = sign_in(client, secret)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/me", headers=HOST).json()["device"] == "iPhone · Safari"
    assert "iPhone on Safari" in loop.announced[0]           # said out loud without the "·"
    assert any(e["type"] == "remote" and "signed in" in e["text"] for e in hub.backlog)
    assert loop.announced and "signed in" in loop.announced[0]
    assert client.post("/api/logout", headers=ORIGIN).status_code == 200
    assert auth.devices() == []


def test_websocket_needs_a_signed_in_phone(phone_rig):
    client, auth, secret, *_ = phone_rig
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws", headers=ORIGIN) as ws:
            ws.receive_json()
    assert e.value.code == 4401
    sign_in(client, secret)
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws", headers={**HOST, "origin": "http://evil.example"}) as ws:
            ws.receive_json()
    assert e.value.code == 4403


def test_phone_requests_run_as_remote_and_pc_control_asks_on_the_phone(phone_rig):
    """Owner's choice: the phone can control the PC, but asks Yes/No on the phone first."""
    client, auth, secret, loop, brain, hub = phone_rig
    sign_in(client, secret)
    with client.websocket_connect("/ws", headers=ORIGIN) as ws:
        hello = recv(ws, "hello")
        assert hello["device"] == "iPhone · Safari"
        ws.send_json({"type": "text", "text": "type hello"})
        ask = recv(ws, "confirm_request")
        assert ask["tool"] == "type_text"
        ws.send_json({"type": "confirm", "id": ask["id"], "approved": False})
        done = recv(ws, "tool_finished")
        assert done["is_error"] and "declined" in done["summary"]
        assert recv(ws, "text")["text"] == "You said type hello."
    text, ctx = brain.seen[0]
    assert ctx.remote and ctx.client_id.startswith("phone:")
    assert ctx.services["grid"] == "GRID" and "teacher" not in ctx.services     # the mouse (asks first), no lessons
    assert ctx.services["memory"] == "MEM"
    assert any(e["type"] == "remote" and "type hello" in e["text"] for e in hub.backlog)
    audit = [json.loads(line) for line in open(ctx.settings.safety.audit_path())]
    assert audit[-1]["tool"] == "type_text" and audit[-1]["remote"] is True


def test_the_conversation_survives_a_reconnect(phone_rig):
    """Phones drop the connection when the screen locks: the chat comes back."""
    client, auth, secret, *_ = phone_rig
    sign_in(client, secret)
    with client.websocket_connect("/ws", headers=ORIGIN) as ws:
        recv(ws, "hello")
        ws.send_json({"type": "text", "text": "hi"})
        recv(ws, "turn_complete")
    with client.websocket_connect("/ws", headers=ORIGIN) as ws:
        backlog = recv(ws, "hello")["backlog"]
        assert [e["type"] for e in backlog][:1] == ["you"] and any(e["type"] == "turn_complete" for e in backlog)


def test_buttons_are_fixed_actions(phone_rig):
    client, auth, secret, loop, brain, hub = phone_rig
    sign_in(client, secret)
    with client.websocket_connect("/ws", headers=ORIGIN) as ws:
        recv(ws, "hello")
        ws.send_json({"type": "action", "name": "stand_down"})
        assert "standing down" in recv(ws, "sys")["text"]
        ws.send_json({"type": "action", "name": "run_shell"})     # not a button: ignored
        ws.send_json({"type": "action", "name": "wake"})
        assert "back on duty" in recv(ws, "sys")["text"]
    assert ("stand_down",) in loop.calls and ("resume",) in loop.calls


def test_signing_out_from_the_pc_drops_the_phone(phone_rig):
    client, auth, secret, *_ = phone_rig
    sign_in(client, secret)
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/ws", headers=ORIGIN) as ws:
            recv(ws, "hello")
            auth.revoke()
            for _ in range(20):
                ws.receive_json()
    assert e.value.code == 4401


def test_confirmations_reach_the_phone_in_plain_words(settings, registry):
    from assistant.core.session import Session
    sent = []

    async def send(m):
        sent.append(m)

    async def go():
        s = Session(FakeBrain(settings, registry), send, remote=True)
        task = asyncio.create_task(s._confirm("delete_file", {"path": "~/Desktop/x.txt"}))
        await asyncio.sleep(0)
        s.resolve_confirm(sent[0]["id"], True)
        return await task
    assert asyncio.run(go()) is True
    assert sent[0]["type"] == "confirm_request" and "x.txt" in sent[0]["text"]


def test_dictation_is_blocked_from_a_phone(registry):
    assert registry.effective_risk("dictation", remote=True).value == "blocked"


def test_device_names_from_the_browser():
    assert R.device_name("Mozilla/5.0 (Linux; Android 14) Chrome/120 Mobile") == "Android phone · Chrome"
    assert R.device_name("Mozilla/5.0 (iPhone) CriOS/120 Safari") == "iPhone · Chrome"
    assert R.device_name("") == "Phone"


# --- the window's Phone page -----------------------------------------------------------------
def test_phone_setup_from_the_window(settings, tmp_path):
    loop = PhoneLoop(None)
    svc = R.PhoneService(settings, loop, hud.Hub())
    app = hud.create_hud_app(settings, FakeLoop(), hud.Hub(), KEY, None, trust_test_client=True, phone=svc)
    client = TestClient(app)
    assert client.get("/phonesetup.js", headers={"host": "127.0.0.1:8766"}).status_code == 200
    ws = connect(client)
    ws.send_json({"type": "phone_get"})
    assert recv(ws, "phone")["configured"] is False
    ws.send_json({"type": "phone_setup", "password": "short"})
    assert "10 characters" in recv(ws, "toast")["text"]
    ws.send_json({"type": "phone_setup", "password": PW})
    shown = recv(ws, "phone_setup")
    assert shown["qr"].startswith("data:image/svg+xml")
    secret = shown["secret"].replace(" ", "")
    ws.send_json({"type": "phone_confirm", "code": A.totp(secret, int(time.time() // 30))})
    recv(ws, "phone_done")
    info = recv(ws, "phone")
    assert info["configured"] and info["enabled"]
    assert PW not in json.dumps(info) and secret not in json.dumps(info)
    ws.send_json({"type": "phone_enable", "on": False})
    assert any(recv(ws, "phone")["enabled"] is False for _ in range(3))
    ws.send_json({"type": "phone_reset"})
    recv(ws, "phone_done")              # (the window also gets periodic "phone" updates)
    assert recv(ws, "phone")["configured"] is False
    ws.close()


async def test_service_listens_only_while_switched_on(settings, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        settings.phone.port = s.getsockname()[1]
    monkeypatch.setattr(R, "tailscale_info", lambda: {"installed": True, "ip": "127.0.0.1", "dns": "pc.tail1.ts.net"})
    svc = R.PhoneService(settings, PhoneLoop(None), hud.Hub())
    await svc._reconcile()
    assert not svc.running                                  # not set up yet: nothing listens
    set_up(svc.auth)
    await svc._reconcile()
    svc_app_hosts = f"pc.tail1.ts.net:{settings.phone.port}"
    assert svc.running and svc_app_hosts in svc.addresses()[0] and svc.addresses() == [f"http://pc.tail1.ts.net:{settings.phone.port}",
                                               f"http://127.0.0.1:{settings.phone.port}"]
    svc.auth.set_enabled(False)
    await svc._reconcile()
    assert not svc.running
    monkeypatch.setattr(R, "tailscale_info", lambda: {"installed": True, "ip": None, "dns": None})
    svc.auth.set_enabled(True)
    await svc._reconcile()
    assert not svc.running and "Waiting" not in svc.error    # Tailscale down: wait, don't fall back


def test_pc_control_off_refuses_from_the_phone(phone_rig, settings):
    settings.phone.pc_control = "off"
    client, auth, secret, loop, brain, hub = phone_rig
    sign_in(client, secret)
    with client.websocket_connect("/ws", headers=ORIGIN) as ws:
        recv(ws, "hello")
        ws.send_json({"type": "text", "text": "type hello"})
        done = recv(ws, "tool_finished")
        assert done["is_error"] and "remote" in done["summary"]


def test_some_things_never_happen_from_the_phone(registry, settings):
    assert registry.effective_risk("type_text", remote=True).value == "confirm"
    assert registry.effective_risk("teach", remote=True).value == "blocked"
    assert registry.effective_risk("voice_lock", remote=True).value == "blocked"
    assert registry.effective_risk("dictation", remote=True).value == "blocked"


# --- talking to Nova from the phone -----------------------------------------------------------------
class FakeSTT:
    def __init__(self, text):
        self.text, self.fed = text, []

    def session(self):
        stt = self

        class S:
            async def feed(self, audio):
                stt.fed.append(audio)

            async def finish(self):
                return type("R", (), {"text": stt.text})()
        return S()


class FakeTTS:
    sample_rate = 24000

    def __init__(self):
        self.said = []

    async def synthesize(self, text):
        import numpy as np
        self.said.append(text)
        yield np.zeros(2400, dtype=np.float32)


def test_a_voice_message_is_heard_answered_and_spoken_back(phone_rig, monkeypatch):
    """Owner: "speak to Nova as if I was sitting here at my computer"."""
    import numpy as np
    from assistant.remote import voice as V
    client, auth, secret, loop, brain, hub = phone_rig
    loop.stt, loop.tts = FakeSTT("what time is it"), FakeTTS()
    monkeypatch.setattr(V, "decode", lambda data: np.zeros(16000, dtype=np.float32))
    assert client.post("/api/voice", content=b"x", headers=ORIGIN).status_code == 401    # signed out
    sign_in(client, secret)
    assert client.post("/api/voice", content=b"x", headers=HOST).status_code == 403      # no Origin
    with client.websocket_connect("/ws", headers=ORIGIN) as ws:
        assert recv(ws, "hello")["voice"] is True
        r = client.post("/api/voice", content=b"fake audio", headers=ORIGIN)
        assert r.status_code == 200 and r.json()["text"] == "what time is it"
        you = recv(ws, "you")
        assert you["voice"] is True and you["text"] == "what time is it"
        audio = recv(ws, "audio")
        wav = base64.b64decode(audio["data"])
        assert audio["mime"] == "audio/wav" and wav[:4] == b"RIFF"
    assert brain.seen[0][0] == "what time is it" and brain.seen[0][1].remote
    assert loop.tts.said == ["You said what time is it."]
    assert any("phone (voice)" in e.get("text", "") for e in hub.backlog)


def test_silence_from_the_phone_does_nothing(phone_rig, monkeypatch):
    import numpy as np
    from assistant.remote import voice as V
    client, auth, secret, loop, brain, hub = phone_rig
    loop.stt, loop.tts = FakeSTT("anything"), FakeTTS()
    monkeypatch.setattr(V, "decode", lambda data: np.zeros(100, dtype=np.float32))
    sign_in(client, secret)
    with client.websocket_connect("/ws", headers=ORIGIN) as ws:
        recv(ws, "hello")
        assert client.post("/api/voice", content=b"x", headers=ORIGIN).json()["text"] == ""
        assert "didn't catch" in recv(ws, "sys")["text"]
    assert brain.seen == [] and loop.stt.fed == []


def test_typed_messages_are_not_spoken_by_the_pc(phone_rig):
    client, auth, secret, loop, brain, hub = phone_rig
    loop.stt, loop.tts = FakeSTT(""), FakeTTS()
    sign_in(client, secret)
    with client.websocket_connect("/ws", headers=ORIGIN) as ws:
        recv(ws, "hello")
        ws.send_json({"type": "text", "text": "hi"})
        recv(ws, "turn_complete")
    assert loop.tts.said == []


def test_spoken_replies_are_as_short_as_at_the_desk(settings):
    from assistant.remote import voice as V
    settings.voice.max_spoken_sentences = 2
    assert V.spoken("One. Two! Three? Four.", settings) == "One. Two!"


async def test_voice_messages_need_https_and_say_how(settings, monkeypatch, tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        settings.phone.port = s.getsockname()[1]
    monkeypatch.setattr(R, "tailscale_info", lambda: {"installed": True, "ip": "127.0.0.1", "dns": "pc.tail1.ts.net"})

    def no_cert(dns, folder):
        raise RuntimeError("HTTPS certificates are not enabled")
    monkeypatch.setattr(R, "tailscale_cert", no_cert)
    svc = R.PhoneService(settings, PhoneLoop(None), hud.Hub())
    set_up(svc.auth)
    await svc._reconcile()
    try:
        assert svc.running and not svc.secure and "HTTPS Certificates" in svc.voice_note
        assert svc.addresses()[0].startswith("http://")
        assert svc.info()["voice_note"] == svc.voice_note
    finally:
        await svc.stop()


async def test_with_a_certificate_the_phone_gets_an_https_address(settings, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        settings.phone.port = s.getsockname()[1]
    monkeypatch.setattr(R, "tailscale_info", lambda: {"installed": True, "ip": "127.0.0.1", "dns": "pc.tail1.ts.net"})
    monkeypatch.setattr(R, "tailscale_cert", lambda dns, folder: ("cert.pem", "key.pem"))
    seen = {}

    class FakeServer:
        def __init__(self, app, sock, ssl=None):
            seen["ssl"] = ssl
            sock.close()

        async def serve(self):
            await asyncio.sleep(0)

        def stop(self):
            pass
    monkeypatch.setattr(hud, "_QuietServer", FakeServer)
    svc = R.PhoneService(settings, PhoneLoop(None), hud.Hub())
    set_up(svc.auth)
    await svc._reconcile()
    try:
        assert svc.secure and svc.voice_note == "" and seen["ssl"] == ("cert.pem", "key.pem")
        assert svc.addresses() == [f"https://pc.tail1.ts.net:{settings.phone.port}"]
    finally:
        await svc.stop()
