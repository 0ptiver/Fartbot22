"""Entry point: python -m assistant {serve|chat|voice|doctor|devices|models|tools|secrets|audit}"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "help"

    if cmd == "serve":
        import uvicorn

        from assistant.core.config import load_settings
        from assistant.core.server import create_app

        s = load_settings()
        uvicorn.run(create_app(s), host=s.server.host, port=s.server.port, log_level="info")
    elif cmd == "chat":
        from assistant.cli import main as chat

        chat(argv)
    elif cmd == "voice":
        from assistant.voice.cli import main as voice

        voice(argv)
    elif cmd == "mictest":
        import asyncio

        from assistant.voice.cli import mictest

        device = None
        if "--input" in argv and argv.index("--input") + 1 < len(argv):
            device = argv[argv.index("--input") + 1]
        asyncio.run(mictest(input_device=device))
    elif cmd == "ttsbench":
        from assistant.voice.cli import ttsbench

        ttsbench(profile="--profile" in argv)
    elif cmd == "devices":
        from assistant.voice.cli import list_devices

        list_devices()
    elif cmd == "models":
        from assistant.core.config import load_settings
        from assistant.voice.models import fetch_all

        s = load_settings()
        fetch_all(s.voice.stt.whisper.model if s.voice.stt.provider == "whisper" else None)
    elif cmd == "doctor":
        from assistant.doctor import main as doctor

        doctor(argv)
    elif cmd == "tools":
        from assistant.core.config import load_settings
        from assistant.tools import build_registry

        s = load_settings()
        reg = build_registry(s)
        for name in reg.names():
            t = reg.get(name)
            print(f"{name:16} {reg.effective_risk(name, False).value:8} {t.description[:70]}")
        if s.brain.web_search.enabled:
            print(f"{'web_search':16} {'safe':8} (Claude server tool)")
    elif cmd == "audit":
        import json

        from assistant.core.config import load_settings
        from assistant.tools.registry import AuditLog

        for rec in AuditLog(load_settings().safety.audit_path()).tail(int(argv[0]) if argv else 20):
            print(json.dumps(rec, ensure_ascii=False))
    elif cmd == "secrets" and len(argv) == 2 and argv[0] == "set":
        import getpass

        from assistant.core.secrets import set_secret

        set_secret(argv[1], getpass.getpass(f"{argv[1]}: "))
        print(f"Stored {argv[1]} in the system credential store.")
    else:
        print(__doc__)
        print("  serve               start the core server")
        print("  chat [--local] [--debug]   text chat (via server, or in-process)")
        print("  voice [--mode ptt|open_mic] [--wav F --out G]   talk by voice")
        print("  devices             list audio devices")
        print("  ttsbench [--profile] find the fastest voice (Kokoro) settings for this PC")
        print("  mictest [--input N] record 4 s, show the level, and transcribe it")
        print("  models              download speech models (VAD, TTS, Whisper)")
        print("  doctor [--full]     check setup (Ollama, Claude Code, models, audio)")
        print("  tools               list tools and risk levels")
        print("  audit [N]           show the last N audit log entries")
        print("  secrets set NAME    store an API key in Windows Credential Manager")


if __name__ == "__main__":
    main()
