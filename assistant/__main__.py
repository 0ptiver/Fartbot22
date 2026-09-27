"""Entry point: python -m assistant {serve|chat|voice|background|autostart|hud|doctor|devices|models|tools|routines|secrets|audit}"""

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
        if "--debug-stft" in argv:
            from assistant.voice.models import kokoro_paths
            from assistant.voice.tts.stft_debug import debug_downstream, debug_stft
            model, voices = kokoro_paths()
            debug_stft(model, voices, s.voice.tts.kokoro.voice, s.voice.tts.kokoro.lang)
            debug_downstream(model, voices, s.voice.tts.kokoro.voice, s.voice.tts.kokoro.lang)
        elif "--recheck" in argv:
            from assistant.voice.models import make_gpu_kokoro
            make_gpu_kokoro(s.voice.tts.kokoro.voice, s.voice.tts.kokoro.lang, recheck=True)
        else:
            fetch_all(s.voice.stt.whisper.model if s.voice.stt.provider == "whisper" else None)
    elif cmd == "spotify":
        import asyncio

        from assistant.integrations import spotify
        sub = argv[0] if argv else "status"
        if sub == "login":
            from assistant.core.secrets import get_secret
            print("One-time setup (2 minutes):")
            print("  1. Open https://developer.spotify.com/dashboard and log in")
            print("  2. Create app: any name/description, Redirect URI  " + spotify.REDIRECT_URI)
            print("     and tick 'Web API'. Save, then open Settings and copy the Client ID")
            client_id = input("Client ID" + (" [press Enter to reuse the saved one]" if get_secret("SPOTIFY_CLIENT_ID") else "") + ": ").strip()
            client_id = client_id or get_secret("SPOTIFY_CLIENT_ID") or ""
            if not client_id:
                print("No Client ID given.")
                return
            print("Opening your browser to approve access...")
            asyncio.run(spotify.login(client_id))
            print("Linked: " + asyncio.run(spotify.Spotify().me()))
        elif sub == "devices":
            async def show():
                import socket
                c = spotify.Spotify()
                devs = await c.devices()
                pick = c.pick_device(devs)
                print(f"This PC is called: {socket.gethostname()}")
                if not devs:
                    print("Spotify reports no devices. Open the Spotify app and sign in.")
                for d in devs:
                    flags = [f for f, on in (("active", d.get("is_active")), ("restricted", d.get("is_restricted"))) if on]
                    mark = "  <- Nova uses this" if pick and d["id"] == pick["id"] else ""
                    print(f"  {d.get('name')} [{d.get('type')}] {' '.join(flags)}{mark}")
                state = await c._call("GET", "/me/player")
                if state:
                    item = state.get("item") or {}
                    print(f"Player: {'playing' if state.get('is_playing') else 'paused'} "
                          f"{item.get('name', '-')} on {(state.get('device') or {}).get('name', '-')}")
                else:
                    print("Player: nothing active")
            asyncio.run(show())
        elif sub == "test":
            # Plays a song the way Nova does and prints what Spotify and Windows each say.
            async def test():
                from assistant.core.config import load_settings
                from assistant.tools import music, video
                from assistant.tools.registry import ToolContext, ToolError
                query = " ".join(argv[1:]) or "Blinding Lights by The Weeknd"
                ctx = ToolContext(load_settings())
                print(f"Asking Nova to play: {query}")
                try:
                    print("Nova says: " + await music.play_music({"query": query}, ctx))
                except ToolError as e:
                    print("Nova says (failed): " + str(e))
                sp = ctx.services.get("spotify")
                print(f"Target: {getattr(sp, 'last_target', None)}")
                print(f"Windows can see media: {video.MEDIA.available()}")
                for m in await video.MEDIA.list():
                    print(f"  Windows: {m.status} '{m.title}' by '{m.artist}' in {m.app}")
                try:
                    state = await sp._call("GET", "/me/player")
                    item = (state or {}).get("item") or {}
                    print(f"Spotify says: {'playing' if (state or {}).get('is_playing') else 'not playing'} "
                          f"'{item.get('name', '-')}' on {((state or {}).get('device') or {}).get('name', '-')}")
                except Exception as e:
                    print(f"Spotify says: error {e}")
            asyncio.run(test())
        elif sub == "logout":
            spotify.logout()
            print("Unlinked. (Also remove the app at spotify.com/account/apps to revoke it fully.)")
        else:
            if spotify.Spotify.linked():
                try:
                    print("Linked: " + asyncio.run(spotify.Spotify().me()))
                except spotify.SpotifyError as e:
                    print(e)
            else:
                print("Not linked. Run: python -m assistant spotify login")
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
    elif cmd == "background":
        from assistant.background import main_supervisor

        main_supervisor()
    elif cmd == "autostart":
        from assistant.background import autostart_enabled, set_autostart

        sub = argv[0] if argv else "status"
        if sub in ("on", "off"):
            print(set_autostart(sub == "on"))
            if sub == "on":
                print("To start it now without restarting: .venv\\Scripts\\pythonw -m assistant background")
        else:
            print("Nova starts with Windows." if autostart_enabled() else "Nova doesn't start with Windows. "
                  "Turn it on: python -m assistant autostart on")
    elif cmd == "hud":
        from assistant.hud.server import URL_FILE, open_window

        try:
            url = URL_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            url = ""
        if not url:
            print("Nova isn't running. Start it with: .venv\\Scripts\\python -m assistant voice")
        else:
            open_window(url)
            print("Opening Nova's window.")
    elif cmd == "routines":
        from assistant.core.config import load_settings
        from assistant.tools import build_registry, routines

        s = load_settings()
        reg = build_registry(s)
        for name, r in routines.active(s).items():
            print(f"{name}: say " + " / ".join(f'"{p}"' for p in r.phrases))
            for step in r.steps:
                print(f"    - {reg.describe(step.tool, step.args)}" if step.tool else f"    - wait {step.wait:g} s")
        found = routines.problems(s, reg)
        print("\n".join(["", "Problems:", *found]) if found else "\nAll routines look fine.")
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
        print("  spotify login|logout|status|devices   link Nova to Spotify / see its devices")
        print("  spotify test [song] play a song like Nova does and show what Spotify and Windows report")
        print("  tools               list tools and risk levels")
        print("  background          run Nova in the tray (no console), restarting it if it crashes")
        print("  autostart on|off    start Nova (in the tray) when you sign in to Windows")
        print("  hud                 reopen Nova's window (while `voice` is running)")
        print("  routines            list your routines and check them for mistakes")
        print("  audit [N]           show the last N audit log entries")
        print("  secrets set NAME    store an API key in Windows Credential Manager")


if __name__ == "__main__":
    main()
