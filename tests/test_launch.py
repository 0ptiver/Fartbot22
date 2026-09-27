"""Apps Nova opens must not be attached to Nova's console (owner's case: Discord's logs
appeared in Nova's window after "gaming mode" opened it)."""

import subprocess

from assistant.core import launch as L


def test_windows_launch_is_detached(monkeypatch):
    calls = []
    monkeypatch.setattr(L.sys, "platform", "win32")
    L.launch("C:/Users/me/Desktop/Discord.lnk", _popen=lambda cmd, **kw: calls.append((cmd, kw)))
    cmd, kw = calls[0]
    assert cmd[-1] == "C:/Users/me/Desktop/Discord.lnk"           # one argument, no shell parsing
    assert "shell" not in kw
    assert kw["creationflags"] & L.DETACHED_PROCESS and kw["creationflags"] & L.CREATE_NO_WINDOW
    assert kw["stdout"] is subprocess.DEVNULL and kw["stderr"] is subprocess.DEVNULL


def test_helper_really_opens_the_target(tmp_path):
    # The helper one-liner is valid Python and passes the target through untouched.
    import sys
    out = subprocess.run([sys.executable, "-c", L._HELPER.replace("os.startfile", "print"), "a b&c"],
                         capture_output=True, text=True)
    assert out.stdout.strip() == "a b&c"
