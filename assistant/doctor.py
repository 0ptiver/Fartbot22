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
            for model in (s.brain.local.model, s.brain.local.vision_model):
                name = model if ":" in model else model + ":latest"
                if name in have:
                    line(OK, f"model {model}")
                else:
                    line(FAIL, f"model {model} not downloaded", f"run: ollama pull {model}")
                    problems += 1
            if full and (s.brain.local.model in have or f"{s.brain.local.model}:latest" in have):
                from assistant.brain.local import LocalBrain
                from assistant.core.conversation import Conversation
                from assistant.tools import build_registry
                from assistant.tools.registry import ToolContext
                import time
                brain = LocalBrain(s, build_registry(s))
                await brain.warm_up()
                t = time.perf_counter()
                reply, first = "", None
                async for ev in brain.run_turn(Conversation(), "Say hello in five words.", ToolContext(s)):
                    if ev.type == "text":
                        first = first or time.perf_counter()
                        reply += ev.text
                if first:
                    line(OK, f"local reply in {(first - t) * 1000:.0f} ms to first word", repr(reply.strip()[:60]))
                else:
                    line(FAIL, "local model gave no reply", getattr(ev, "message", ""))
                    problems += 1
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

    # --- Security -----------------------------------------------------------------
    from assistant.core.secrets import get_secret, local_client_token
    local_client_token()
    line(OK, "local client token stored")
    if s.brain.backend == "anthropic" or s.brain.expert.backend == "anthropic":
        line(OK if get_secret("ANTHROPIC_API_KEY") else FAIL, "ANTHROPIC_API_KEY (needed for the API backend)")
    elif get_secret("ANTHROPIC_API_KEY"):
        line(WARN, "an ANTHROPIC_API_KEY is stored but not used",
             "fine; Nova strips it when calling Claude Code so your subscription is used")

    print("\nAll good." if not problems else f"\n{problems} problem(s) to fix.")
    return problems


def main(argv: list[str]) -> None:
    sys.exit(1 if asyncio.run(check("--full" in argv)) else 0)
