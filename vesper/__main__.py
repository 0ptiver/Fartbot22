"""Entry point: python -m vesper {serve|chat|tools|secrets|audit}"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "help"

    if cmd == "serve":
        import uvicorn

        from vesper.core.config import load_settings
        from vesper.core.server import create_app

        s = load_settings()
        uvicorn.run(create_app(s), host=s.server.host, port=s.server.port, log_level="info")
    elif cmd == "chat":
        from vesper.cli import main as chat

        chat(argv)
    elif cmd == "tools":
        from vesper.core.config import load_settings
        from vesper.tools import build_registry

        s = load_settings()
        reg = build_registry(s)
        for name in reg.names():
            t = reg.get(name)
            print(f"{name:16} {reg.effective_risk(name, False).value:8} {t.description[:70]}")
        if s.brain.web_search.enabled:
            print(f"{'web_search':16} {'safe':8} (Claude server tool)")
    elif cmd == "audit":
        import json

        from vesper.core.config import load_settings
        from vesper.tools.registry import AuditLog

        for rec in AuditLog(load_settings().safety.audit_path()).tail(int(argv[0]) if argv else 20):
            print(json.dumps(rec, ensure_ascii=False))
    elif cmd == "secrets" and len(argv) == 2 and argv[0] == "set":
        import getpass

        from vesper.core.secrets import set_secret

        set_secret(argv[1], getpass.getpass(f"{argv[1]}: "))
        print(f"Stored {argv[1]} in the system credential store.")
    else:
        print(__doc__)
        print("  serve               start the core server")
        print("  chat [--local] [--debug]   text chat (via server, or in-process)")
        print("  tools               list tools and risk levels")
        print("  audit [N]           show the last N audit log entries")
        print("  secrets set NAME    store an API key in Windows Credential Manager")


if __name__ == "__main__":
    main()
