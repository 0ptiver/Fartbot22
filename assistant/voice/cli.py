"""`python -m assistant voice` — talk to the assistant through the mic and speakers."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

DIM, CYAN, GREEN, RED, RESET = "\033[2m", "\033[36m", "\033[32m", "\033[31m", "\033[0m"


def make_printer(name: str, show_latency: bool):
    state = {"speaking": False}

    def on_event(ev: dict) -> None:
        t = ev["type"]
        if t == "listening":
            print(f"\n{GREEN}● listening…{RESET}", flush=True)
        elif t == "transcript":
            print(f"you: {ev['text']}", flush=True)
        elif t == "text":
            if not state["speaking"]:
                print(f"{CYAN}{name}:{RESET} ", end="", flush=True)
                state["speaking"] = True
            print(ev["text"], end="", flush=True)
        elif t == "tool":
            print(f"\n{DIM}  ⚙ {ev['name']}{RESET}", flush=True)
            state["speaking"] = False
        elif t == "error":
            print(f"\n{RED}{ev['message']}{RESET}", flush=True)
        elif t == "interrupted":
            print(f"\n{DIM}  (interrupted){RESET}", flush=True)
            state["speaking"] = False
        elif t == "latency":
            print()
            state["speaking"] = False
            if show_latency:
                print(DIM + ev["report"] + RESET, flush=True)
        elif t == "idle" and ev.get("reason"):
            print(f"{DIM}  ({ev['reason']}){RESET}", flush=True)

    return on_event


async def build(settings, wav: str | None, out: str | None):
    from assistant.brain.llm import Brain
    from assistant.tools import build_registry
    from assistant.voice.audio import AudioPlayer, MicStream, RecordingPlayer, WavMic
    from assistant.voice.hotkey import PushToTalk
    from assistant.voice.models import fetch_silero
    from assistant.voice.stt.base import create_stt
    from assistant.voice.tts.base import create_tts
    from assistant.voice.vad import SileroVAD

    vcfg = settings.voice
    brain = Brain(settings, build_registry(settings))
    stt, tts = create_stt(settings), create_tts(settings)
    t0 = time.perf_counter()
    print(f"Loading models (STT: {vcfg.stt.provider}, TTS: {vcfg.tts.provider})…", flush=True)
    vad_path = await asyncio.to_thread(fetch_silero)
    await asyncio.gather(stt.load(), tts.load())
    print(f"Models ready in {time.perf_counter() - t0:.1f}s"
          + (f" (whisper on {stt.device})" if getattr(stt, "device", None) else ""), flush=True)
    vad = SileroVAD(vad_path)

    if wav:
        settings.voice.mode = "open_mic"
        mic, player, ptt = WavMic(wav, realtime=True), RecordingPlayer(tts.sample_rate), None
    else:
        mic = MicStream(vcfg.input_device)
        player = AudioPlayer(tts.sample_rate, vcfg.output_device)
        ptt = PushToTalk(vcfg.ptt_hotkey) if vcfg.mode == "ptt" else None
    return brain, stt, tts, mic, player, vad, ptt


async def run(args) -> None:
    from assistant.core.config import load_settings
    from assistant.voice.pipeline import VoiceLoop

    settings = load_settings()
    if args.mode:
        settings.voice.mode = args.mode
    brain, stt, tts, mic, player, vad, ptt = await build(settings, args.wav, args.out)
    loop = VoiceLoop(settings, brain, stt, tts, mic, player, vad, ptt,
                     make_printer(settings.assistant.name, settings.voice.latency_report or args.debug))
    if ptt:
        ptt.start()
        print(f"Hold {settings.voice.ptt_hotkey.upper()} and speak. Release to send. Ctrl+C to quit.")
    elif not args.wav:
        print("Open mic: just talk. (Use headphones until echo cancellation lands in Phase 3.) Ctrl+C to quit.")
    try:
        await loop.run(max_turns=1 if args.wav else None)
    finally:
        mic.close()
        player.close()
        if ptt:
            ptt.stop()
    if args.wav and args.out:
        player.save(args.out)
        print(f"Saved reply audio to {args.out}")


def list_devices() -> None:
    import sounddevice as sd

    print(sd.query_devices())
    print(f"\nDefault input/output: {sd.default.device}")


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="assistant voice")
    p.add_argument("--mode", choices=["ptt", "open_mic"], help="override voice.mode from config")
    p.add_argument("--wav", help="use a WAV file as the microphone (one turn)")
    p.add_argument("--out", help="with --wav: save the spoken reply to this WAV")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.debug else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nGoodbye.")


if __name__ == "__main__":
    main(sys.argv[1:])
