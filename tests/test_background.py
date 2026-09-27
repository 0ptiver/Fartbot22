"""Always-on Nova: supervisor restarts, autostart (registry faked), log file, one instance, tray menu."""

import asyncio
import logging

import pytest

from assistant import background as bg
from assistant import tray as T


def test_supervisor_restarts_with_backoff_and_stops_on_quit():
    codes = iter([1, 1, 1, bg.EXIT_RESTART, 1, bg.EXIT_QUIT])
    waits = []
    t = [0.0]

    def spawn():
        t[0] += 1                     # each run lasts a second
        return next(codes)
    assert bg.supervise(spawn, sleep=waits.append, clock=lambda: t[0]) == 0
    assert waits == [5.0, 10.0, 20.0, 5.0]           # doubled after crashes; reset by Restart


def test_supervisor_caps_the_wait_and_resets_after_a_long_run():
    t = [0.0]
    runs = iter([1] * 6 + [1])
    lengths = iter([1, 1, 1, 1, 1, 1, 700])
    waits = []

    def spawn():
        t[0] += next(lengths)
        return next(runs)
    bg.supervise(spawn, sleep=waits.append, clock=lambda: t[0], max_runs=7)
    assert waits[:6] == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0] and waits[6] == 5.0


class FakeReg:
    HKEY_CURRENT_USER, REG_SZ = "HKCU", 1

    def __init__(self):
        self.values = {}

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, root, path):
        return self._Key()

    CreateKey = OpenKey

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise OSError("missing")
        return self.values[name], 1

    def SetValueEx(self, key, name, _r, _t, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise OSError("missing")
        del self.values[name]


def test_autostart_on_off():
    reg = FakeReg()
    assert not bg.autostart_enabled(reg)
    assert "sign in" in bg.set_autostart(True, reg)
    assert bg.autostart_enabled(reg) and reg.values["Nova"].endswith('-m assistant background')
    bg.set_autostart(False, reg)
    bg.set_autostart(False, reg)                     # twice is fine
    assert not bg.autostart_enabled(reg)


def test_log_stream_strips_colours_and_splits_lines(tmp_path):
    logger = logging.getLogger("test.console")
    logger.propagate = False
    lines = []

    class H(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())
    logger.addHandler(H())
    logger.setLevel(logging.INFO)
    s = bg._LogStream(logger, logging.INFO)
    print("\033[36mNova:\033[0m Hello,", end=" ", file=s)
    print("sir.\nsecond line", file=s)
    assert lines == ["Nova: Hello, sir.", "second line"]


def test_single_instance(monkeypatch, tmp_path):
    monkeypatch.setattr(bg, "ROOT", tmp_path)
    assert bg.claim_single_instance()
    first = bg._mutex_handle
    import fcntl
    other = open(tmp_path / "data" / "nova.lock", "w")
    with pytest.raises(OSError):
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)   # a second Nova can't take it
    first.close()


class FakeVoice:
    def __init__(self):
        self.mic_muted, self.standby, self.calls = False, False, []

    async def stand_down(self):
        self.calls.append("stand_down")

    async def resume(self):
        self.calls.append("resume")


async def test_tray_actions_reach_the_voice_loop():
    v, quits, opened = FakeVoice(), [], []
    a = T.TrayActions(v, asyncio.get_running_loop(), lambda: opened.append(1), quits.append)
    await asyncio.to_thread(a.toggle_mute)           # menu clicks come from the tray's thread
    await asyncio.to_thread(a.toggle_standby)
    await asyncio.sleep(0.05)
    assert v.mic_muted and v.calls == ["stand_down"]
    await asyncio.to_thread(a.restart)
    await asyncio.to_thread(a.exit)
    await asyncio.sleep(0.05)
    assert quits == [bg.EXIT_RESTART, bg.EXIT_QUIT]


def test_tray_icon_images():
    for state in T.COLORS:
        img = T.icon_image(state)
        assert img.size == (64, 64) and img.getpixel((32, 32))[:3] == T.COLORS[state]
    tray = T.Tray(None)
    tray.set_state("thinking")                        # no icon (no tray support): just remembers
    assert tray.state == "thinking"


async def test_background_run_quits_and_restarts_from_the_tray(settings, registry, monkeypatch):
    """The whole `voice --background` path with fakes: tray Quit -> exit code 0, Restart -> 3."""
    import argparse

    import numpy as np

    from assistant.brain.llm import Brain
    from assistant.voice import cli
    from assistant.voice.audio import RecordingPlayer
    from tests.fakes import FakeClient
    from tests.voice_helpers import FakeSTT, FakeTTS

    class SilentMic:
        async def frames(self):
            while True:
                yield np.zeros(512, np.float32), 0.0
                await asyncio.sleep(0.005)

        def close(self):
            pass

    async def fake_build(s, wav, out):
        return (Brain(s, registry, FakeClient([])), FakeSTT(), FakeTTS(), SilentMic(), RecordingPlayer(),
                lambda f: 0.0, None)

    trays = []

    class FakeTray:
        def __init__(self, actions, name):
            self.actions, self.states = actions, []
            trays.append(self)

        def start(self):
            pass

        def set_state(self, s):
            self.states.append(s)

        def stop(self):
            pass

    monkeypatch.setattr(cli, "build", fake_build)
    monkeypatch.setattr("assistant.core.config.load_settings", lambda: settings)
    monkeypatch.setattr("assistant.tray.Tray", FakeTray)
    args = argparse.Namespace(mode=None, input=None, output=None, wav=None, out=None, debug=False,
                              no_hud=True, background=True)
    for i, (action, code) in enumerate((("exit", 0), ("restart", 3))):
        task = asyncio.create_task(cli.run(args))
        for _ in range(200):
            if len(trays) == i + 1 and trays[-1].states:
                break
            await asyncio.sleep(0.01)
        await asyncio.to_thread(getattr(trays[-1].actions, action))
        assert await asyncio.wait_for(task, 5) == code
    assert trays[0].states[0] == "idle"
