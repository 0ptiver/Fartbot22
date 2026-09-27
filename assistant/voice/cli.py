"""`python -m assistant voice` — talk to the assistant through the mic and speakers."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

DIM, CYAN, GREEN, RED, RESET = "\033[2m", "\033[36m", "\033[32m", "\033[31m", "\033[0m"


def _print_devices(vcfg) -> None:
    try:
        import sounddevice as sd
        mic = sd.query_devices(vcfg.input_device, "input")["name"]
        out = sd.query_devices(vcfg.output_device, "output")["name"]
        print(f"Mic: {mic}   |   Output: {out}")
    except Exception as e:
        print(f"{RED}Audio device problem: {e}{RESET}\nRun `python -m assistant devices` and pick another.")
        raise


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
            hint = f": {ev['hint']}" if ev.get("hint") else ""
            print(f"{DIM}  ({ev['reason']}{hint}){RESET}", flush=True)

    return on_event


async def build(settings, wav: str | None, out: str | None):
    from assistant.brain import create_brain

    from assistant.voice.audio import AudioPlayer, MicStream, RecordingPlayer, WavMic
    from assistant.voice.hotkey import PushToTalk
    from assistant.voice.models import fetch_silero
    from assistant.voice.stt.base import create_stt
    from assistant.voice.tts.base import create_tts
    from assistant.voice.vad import SileroVAD

    vcfg = settings.voice
    brain = create_brain(settings)
    stt, tts = create_stt(settings), create_tts(settings)
    t0 = time.perf_counter()
    print(f"Loading models (STT: {vcfg.stt.provider}, TTS: {vcfg.tts.provider})…", flush=True)
    vad_path = await asyncio.to_thread(fetch_silero)
    await asyncio.gather(stt.load(), tts.load(), brain.warm_up())
    if not wav:
        _print_devices(vcfg)
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
    for attr in ("input", "output"):
        value = getattr(args, attr)
        if value is not None:
            setattr(settings.voice, f"{attr}_device", int(value) if value.isdigit() else value)
    try:
        brain, stt, tts, mic, player, vad, ptt = await build(settings, args.wav, args.out)
    except Exception as e:
        print(f"{RED}Startup failed: {e}{RESET}\nRun `python -m assistant doctor` for a full check.")
        return
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

    apis = sd.query_hostapis()
    default_in, default_out = sd.default.device
    for kind, key, default in (("MICROPHONES (input)", "max_input_channels", default_in),
                               ("SPEAKERS / HEADPHONES (output)", "max_output_channels", default_out)):
        print(f"\n{kind}")
        for i, d in enumerate(sd.query_devices()):
            if d[key] <= 0:
                continue
            api = apis[d["hostapi"]]["name"]
            mark = "  <- Windows default" if i == default else ""
            print(f"  {i:>3}  {d['name']}  [{api}]{mark}")
    print("\nThe same device shows up once per Windows audio system. Prefer the [MME] entry:"
          "\nit accepts any sample rate. Set it in config/config.yaml, e.g.  input_device: 3"
          "\nor try one first:  python -m assistant voice --input 3")


async def mictest(seconds: float = 4.0, input_device: str | None = None) -> None:
    """Record a few seconds with a live level meter, then transcribe it."""
    import numpy as np

    from assistant.core.config import load_settings
    from assistant.voice.audio import MicStream
    from assistant.voice.stt.base import create_stt

    s = load_settings()
    if input_device is not None:
        s.voice.input_device = int(input_device) if input_device.isdigit() else input_device
    mic = MicStream(s.voice.input_device)
    try:
        import sounddevice as sd
        print(f"Mic: {sd.query_devices(s.voice.input_device, 'input')['name']}")
    except Exception as e:
        print(f"{RED}{e}{RESET}")
        return
    stt = create_stt(s)
    load = asyncio.create_task(stt.load())
    print(f"Say something for {seconds:.0f} seconds, e.g. 'Nova, what time is it?'\n")
    frames, t_end = [], None
    async for frame, t in mic.frames():
        t_end = t_end or t + seconds
        frames.append(frame)
        level = float(np.max(np.abs(frame)))
        bar = "#" * min(40, int(level * 80))
        print(f"\r  level [{bar:<40}] {level:5.1%}", end="", flush=True)
        if t >= t_end:
            break
    mic.close()
    audio = np.concatenate(frames)
    peak = float(np.max(np.abs(audio)))
    print(f"\n\nPeak level: {peak:.1%}", end="  ")
    if peak < 0.01:
        print(f"{RED}-> silent. Wrong mic or muted. Run `assistant devices` and try another --input.{RESET}")
    elif peak < 0.05:
        print("-> quiet but usable (Nova boosts it). Raising the mic level in Windows sound settings helps.")
    else:
        print(f"{GREEN}-> good{RESET}")
    await load
    print("Transcribing…")
    text = (await stt.transcribe(audio)).text if hasattr(stt, "transcribe") else ""
    print(f"Whisper heard: {text!r}" if text else f"{RED}Whisper heard nothing.{RESET}")


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="assistant voice")
    p.add_argument("--mode", choices=["ptt", "open_mic"], help="override voice.mode from config")
    p.add_argument("--input", help="microphone: number or part of the name (see: assistant devices)")
    p.add_argument("--output", help="speakers/headphones: number or part of the name")
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
