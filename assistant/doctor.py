"""`python -m assistant doctor [--full]` — check everything Nova needs and say how to fix it."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys

OK, WARN, FAIL = "\033[32m✓\033[0m", "\033[33m!\033[0m", "\033[31m✗\033[0m"


def line(mark: str, what: str, detail: str = "") -> None:
    print(f" {mark} {what}" + (f"  — {detail}" if detail else ""))


async def check(full: bool) -> int:
    import httpx

    from assistant.core.config import load_settings
    from assistant.voice.models import kokoro_paths, silero_path

    s = load_settings()
    problems = 0
    print(f"Nova doctor  (brain: {s.brain.backend}, hard tasks: {s.brain.expert.backend})\n")

    line(OK if sys.version_info >= (3, 12) else WARN, f"Python {sys.version.split()[0]}")

    # --- Ollama ------------------------------------------------------------------
    if s.brain.backend == "local" or s.brain.local.vision == "ollama":
        try:
            async with httpx.AsyncClient(base_url=s.brain.local.host, timeout=3) as c:
                tags = (await c.get("/api/tags")).json()
                version = (await c.get("/api/version")).json().get("version", "?")
            have = {m["name"] for m in tags.get("models", [])}
            line(OK, f"Ollama {version} running at {s.brain.local.host}")
            for model, need in ((s.brain.local.model, "tools"), (s.brain.local.vision_model, "vision")):
                name = model if ":" in model else model + ":latest"
                if name not in have:
                    line(FAIL, f"model {model} not downloaded", f"run: ollama pull {model}")
                    problems += 1
                    continue
                async with httpx.AsyncClient(base_url=s.brain.local.host, timeout=10) as c:
                    caps = (await c.post("/api/show", json={"model": model})).json().get("capabilities", [])
                if caps and need not in caps:
                    line(FAIL, f"model {model} has no '{need}' support", f"capabilities: {caps}")
                    problems += 1
                elif "thinking" in caps and "instruct" not in model and need == "tools":
                    line(WARN, f"model {model} is a 'thinking' model",
                         "slow to answer out loud; use qwen3:4b-instruct-2507-q4_K_M "
                         "(ollama pull it, then set brain.local.model)")
                else:
                    line(OK, f"model {model}")
            if full and (s.brain.local.model in have or f"{s.brain.local.model}:latest" in have):
                from assistant.brain.local import LocalBrain
                from assistant.core.conversation import Conversation
                from assistant.tools import build_registry
                from assistant.tools.registry import ToolContext
                import time
                brain = LocalBrain(s, build_registry(s))
                t = time.perf_counter()
                await brain.warm_up()
                line(OK, f"model loaded + prompt cached in {time.perf_counter() - t:.1f}s")
                conv = Conversation()
                for label, prompt in (("first reply", "Say hello in five words."),
                                      ("next reply", "What is two plus two?")):
                    t = time.perf_counter()
                    reply, first, err = "", None, ""
                    async for ev in brain.run_turn(conv, prompt, ToolContext(s)):
                        if ev.type == "text":
                            first = first or time.perf_counter()
                            reply += ev.text
                        elif ev.type == "error":
                            err = ev.message
                    if first:
                        ms = (first - t) * 1000
                        mark = OK if ms < 1000 else WARN
                        hint = "" if ms < 1000 else " (slow: is the GPU busy, or VRAM full? check `ollama ps`)"
                        line(mark, f"{label}: {ms:.0f} ms to first word{hint}", repr(reply.strip()[:60]))
                    else:
                        line(FAIL, "local model gave no reply", err)
                        problems += 1
                        break
                vl = s.brain.local.vision_model
                if s.brain.local.vision == "claude_code":
                    import base64
                    import io

                    from PIL import Image, ImageDraw
                    img = Image.new("RGB", (1920, 1080), "white")
                    ImageDraw.Draw(img).text((800, 500), "Error: CommandNotFoundException", fill="red")
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG")
                    t = time.perf_counter()
                    try:
                        desc = await brain.describe_image(base64.b64encode(buf.getvalue()).decode())
                        line(OK, f"screen reading via Claude: {time.perf_counter() - t:.1f}s", repr(desc[:50]))
                    except Exception as e:
                        line(FAIL, "screen reading via Claude failed", str(e)[:100])
                        problems += 1
                elif s.brain.local.vision == "ollama" and (vl in have or f"{vl}:latest" in have):
                    import base64
                    import io

                    from PIL import Image, ImageDraw
                    img = Image.new("RGB", (1920, 1080), "white")
                    ImageDraw.Draw(img).text((800, 500), "Error: CommandNotFoundException", fill="red")
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG")
                    t = time.perf_counter()
                    try:
                        desc = await brain.describe_image(base64.b64encode(buf.getvalue()).decode())
                        st = brain.last_vision_stats
                        secs = time.perf_counter() - t
                        line(OK if secs < 8 else WARN,
                             f"screen reading: {secs:.1f}s (load {st['load_duration'] / 1000:.1f}s, "
                             f"image {st['prompt_eval_duration'] / 1000:.1f}s, answer {st['eval_duration'] / 1000:.1f}s)",
                             repr(desc[:50]))
                    except Exception as e:
                        line(FAIL, "screen reading failed", str(e)[:100])
                    t = time.perf_counter()
                    async for ev in brain.run_turn(conv, "Say OK.", ToolContext(s)):
                        if ev.type == "text":
                            break
                    ms = (time.perf_counter() - t) * 1000
                    line(OK if ms < 1000 else WARN, f"chat reply after screen reading: {ms:.0f} ms",
                         "" if ms < 1000 else "the vision model pushed the chat model off the GPU. "
                         "Consider brain.local.vision: claude_code")
                async with httpx.AsyncClient(base_url=s.brain.local.host, timeout=5) as c:
                    for m in (await c.get("/api/ps")).json().get("models", []):
                        on_gpu = m.get("size_vram", 0) / max(m.get("size", 1), 1)
                        mark = OK if on_gpu > 0.99 else WARN
                        line(mark, f"loaded: {m['name']}  {on_gpu:.0%} on GPU",
                             "" if on_gpu > 0.99 else "part of it runs on the CPU (slow). Free VRAM or use a smaller model")
        except httpx.ConnectError:
            line(FAIL, "Ollama isn't running", "start the Ollama app (it lives in the system tray)")
            problems += 1

    # --- Claude Code (subscription) ---------------------------------------------------
    if s.brain.expert.backend == "claude_code" or s.brain.local.vision == "claude_code":
        exe = shutil.which(s.brain.expert.claude_code.command)
        if not exe:
            line(FAIL, "Claude Code not installed",
                 "PowerShell: irm https://claude.ai/install.ps1 | iex   then run `claude` once and log in")
            problems += 1
        else:
            try:
                v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
                line(OK, f"Claude Code {v}", exe)
            except Exception as e:
                line(WARN, "Claude Code found but --version failed", str(e))
            if full:
                from assistant.brain.expert import ClaudeCodeExpert, ExpertError
                import time
                t = time.perf_counter()
                try:
                    ans = await ClaudeCodeExpert(s).ask("Reply with exactly the word OK and nothing else.")
                    line(OK, f"Claude subscription works ({time.perf_counter() - t:.1f}s)", repr(ans[:40]))
                except (ExpertError, asyncio.TimeoutError) as e:
                    line(FAIL, "Claude Code test failed", str(e) or "timed out")
                    problems += 1
            else:
                line(WARN, "sign-in not tested", "run `python -m assistant doctor --full` to test it (uses a tiny bit of your plan)")

    # --- Speech -------------------------------------------------------------------
    line(OK if silero_path().exists() else FAIL, "Silero VAD model", "" if silero_path().exists() else "run: python -m assistant models")
    m, v = kokoro_paths()
    ok = m.exists() and v.exists()
    line(OK if ok else FAIL, "Kokoro TTS model", "" if ok else "run: python -m assistant models")
    problems += (not silero_path().exists()) + (not ok)
    try:
        from assistant.voice.tts.kokoro import cuda_available
        if cuda_available():
            line(OK, "voice (Kokoro) can run on the GPU")
        elif shutil.which("nvidia-smi"):
            line(WARN, "voice (Kokoro) runs on the CPU (~600 ms per sentence)",
                 "for the GPU: powershell -ExecutionPolicy Bypass -File scripts\\enable_gpu_tts.ps1")
    except Exception as e:
        line(WARN, "couldn't check onnxruntime", str(e)[:80])
    if shutil.which("nvidia-smi"):
        try:
            gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total",
                                  "--format=csv,noheader"], capture_output=True, text=True, timeout=10).stdout.strip()
            line(OK, f"GPU {gpu}")
        except Exception:
            line(WARN, "nvidia-smi failed")
    else:
        line(WARN, "no NVIDIA GPU found", "speech-to-text will run on the CPU (slower)")
    try:
        import sounddevice as sd
        dev = sd.query_devices(kind="input")
        line(OK, f"microphone: {dev['name']}")
    except Exception as e:
        line(FAIL, "no microphone / audio system", str(e)[:80])
        problems += 1

    try:
        from assistant.voice.aec import EchoCanceller
        EchoCanceller()
        line(OK, "echo cancellation (WebRTC AEC3) available")
    except Exception:
        line(WARN, "echo cancellation unavailable", "pip install livekit  (Nova may hear itself on speakers)")

    if sys.platform == "win32":
        try:
            import importlib
            importlib.import_module("winrt.windows.foundation.collections")   # get_sessions needs it
            from assistant.tools.video import MediaBackend, apartment
            n = await MediaBackend().count()
            if n is None:
                line(WARN, "Windows' media list isn't answering",
                     f"restart the browser or Spotify; Nova carries on without it (main thread COM: {apartment()})")
            else:
                line(OK, f"video control ready (Windows lists {n} media session{'s' if n != 1 else ''})")
        except Exception as e:
            line(WARN, "video control limited (play/pause falls back to the media key)",
                 f"{type(e).__name__}: {e}. Run scripts\\update.ps1")
        try:
            import asyncio
            import time as _time

            from assistant.tools import ocr
            started = _time.perf_counter()
            _, lines = await asyncio.to_thread(ocr.read_screen, "screen", 1)
            line(OK, f"Nova reads the screen itself ({len(lines)} lines of text on screen 1, "
                     f"{(_time.perf_counter() - started) * 1000:.0f} ms)")
        except Exception as e:
            line(WARN, "Nova can't read the screen itself (Claude looks instead)",
                 f"{type(e).__name__}: {str(e)[:120]}")

    from assistant.voice.voiceprint import WEIGHTS_FILE, VoiceLock
    lock = VoiceLock()
    if not WEIGHTS_FILE.exists():
        line(WARN, "voice lock model not downloaded", "python -m assistant models")
    elif lock.prints.enrolled:
        line(OK, f"voice lock {'on' if lock.on else 'off'} (your voice is learned; bar {lock.threshold:.2f})")
    else:
        line(OK, "voice lock ready (say \"Nova, learn my voice\" to turn it on)")

    try:
        from assistant.brain.agent import Agent
        agent = Agent(s)
        if not s.brain.agent.enabled:
            line(OK, "work it out (Claude with Nova's tools): off")
        elif agent.available():
            line(OK, "work it out ready: when Nova is stuck, Claude does it with Nova's tools"
                 + ("" if s.brain.agent.on_failure else " (only when you say \"figure it out\")"))
        else:
            line(WARN, "work it out unavailable", "needs Claude Code installed and signed in (run `claude` once)")
    except Exception as e:
        line(WARN, "work it out check failed", str(e)[:120])

    from assistant.integrations.spotify import Spotify
    line(OK if Spotify.linked() else WARN, "Spotify " + ("linked" if Spotify.linked() else "not linked"),
         "" if Spotify.linked() else "for music control: python -m assistant spotify login")

    # --- Security -----------------------------------------------------------------
    from assistant.core.secrets import get_secret, local_client_token
    local_client_token()
    line(OK, "local client token stored")
    import importlib.util
    if importlib.util.find_spec("playwright") is None:
        line(WARN, "Nova's browser not installed", "run scripts\\update.ps1")
    else:
        line(OK, "Nova's browser ready (drives Microsoft Edge with its own profile)"
             + ("" if s.browser.handle_websites else "; websites open in your normal browser"))
    from assistant.remote.auth import PhoneAuth
    from assistant.remote.server import tailscale_info
    phone = PhoneAuth(s.phone.session_days)
    if not phone.configured:
        line(OK, "phone access not set up (the window's Phone page sets it up)")
    elif not phone.enabled:
        line(OK, "phone access set up, switched off")
    else:
        ts = tailscale_info()
        line(OK if ts["ip"] else WARN, f"phone access on ({len(phone.devices())} signed in)" +
             (f": http://{ts['dns'] or ts['ip']}:{s.phone.port}" if ts["ip"] else ""),
             "" if ts["ip"] else "Tailscale isn't connected on this PC: open Tailscale and sign in")
    if s.brain.backend == "anthropic" or s.brain.expert.backend == "anthropic":
        line(OK if get_secret("ANTHROPIC_API_KEY") else FAIL, "ANTHROPIC_API_KEY (needed for the API backend)")
    elif get_secret("ANTHROPIC_API_KEY"):
        line(WARN, "an ANTHROPIC_API_KEY is stored but not used",
             "fine; Nova strips it when calling Claude Code so your subscription is used")

    print("\nAll good." if not problems else f"\n{problems} problem(s) to fix.")
    return problems


def main(argv: list[str]) -> None:
    sys.exit(1 if asyncio.run(check("--full" in argv)) else 0)
