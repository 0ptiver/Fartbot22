"""`python -m assistant voice` — talk to the assistant through the mic and speakers."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

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
            print(f"\n{DIM}  ⚙ {ev['name']}…{RESET}", flush=True)
            state["speaking"] = False
        elif t == "tool_done":
            mark = f"{RED}✗" if ev["is_error"] else "✓"
            extra = f": {ev['summary'][:100]}" if ev["is_error"] else ""
            print(f"{DIM}  {mark} {ev['name']} took {ev['ms'] / 1000:.1f}s{extra}{RESET}", flush=True)
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
        elif t == "ignored":
            print(f"{DIM}  (heard \"{ev['text']}\": {ev['reason']}){RESET}", flush=True)
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
    if vcfg.mode == "wake" and not vcfg.stt.whisper.hotwords:
        vcfg.stt.whisper.hotwords = settings.assistant.name   # helps Whisper spell "Nova"
    brain = create_brain(settings)
    stt, tts = create_stt(settings), create_tts(settings)
    t0 = time.perf_counter()
    print(f"Loading models (STT: {vcfg.stt.provider}, TTS: {vcfg.tts.provider})…", flush=True)
    vad_path = await asyncio.to_thread(fetch_silero)
    await asyncio.gather(stt.load(), tts.load(), brain.warm_up())
    if not wav:
        _print_devices(vcfg)
    where = [f"{label} on {dev}" for label, dev in (("whisper", getattr(stt, "device", None)),
                                                    ("voice", getattr(tts, "device", None))) if dev]
    print(f"Models ready in {time.perf_counter() - t0:.1f}s"
          + (f" ({', '.join(where)})" if where else ""), flush=True)
    vad = SileroVAD(vad_path)

    if wav:
        settings.voice.mode = "open_mic" if settings.voice.mode == "ptt" else settings.voice.mode
        mic, player, ptt = WavMic(wav, realtime=True), RecordingPlayer(tts.sample_rate), None
    else:
        mic = MicStream(vcfg.input_device)
        player = AudioPlayer(tts.sample_rate, vcfg.output_device)
        ptt = PushToTalk(vcfg.ptt_hotkey) if vcfg.mode == "ptt" else None
        player.start()   # open the speakers now, not on the first word (saves ~0.4 s)
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
    await loop.prewarm()
    name = settings.assistant.name
    if ptt:
        ptt.start()
        print(f"Hold {settings.voice.ptt_hotkey.upper()} and speak. Release to send. Ctrl+C to quit.")
    elif settings.voice.mode == "wake" and not args.wav:
        print(f"Listening. Say \"{name}, ...\" (e.g. \"{name}, what time is it?\"). "
              f"After a reply you can answer without the name for "
              f"{settings.voice.wake.follow_up_s:.0f}s. Ctrl+C to quit.")
    elif not args.wav:
        print("Open mic: answers everything it hears. Ctrl+C to quit.")
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


def ttsbench(profile: bool = False) -> None:
    """Time Kokoro on GPU and CPU (threads, int8 model) and recommend the fastest."""
    import os
    import statistics

    from kokoro_onnx import Kokoro

    from assistant.core.config import load_settings
    from assistant.voice.models import KOKORO_GPU, kokoro_paths
    from assistant.voice.tts.kokoro import cuda_available, make_session

    logging.getLogger("phonemizer").setLevel(logging.ERROR)
    k = load_settings().voice.tts.kokoro
    phrases = ["Certainly, sir.", "Something along those lines, sir.",
               "The time is a quarter past eleven."]
    cores = os.cpu_count() or 8
    gpu = cuda_available()
    print(f"Voice {k.voice}. GPU (onnxruntime CUDA): {'yes' if gpu else 'no'}. Lower is better.\n")

    runs = []  # (label, model file, device, threads, cuDNN search)
    if gpu:
        runs += [("GPU heuristic", "kokoro-v1.0.onnx", "cuda", None, "heuristic"),
                 ("GPU default", "kokoro-v1.0.onnx", "cuda", None, "default"),
                 ("GPU exhaustive", "kokoro-v1.0.onnx", "cuda", None, "exhaustive")]
        if kokoro_paths(KOKORO_GPU)[0].exists():
            runs.insert(0, ("GPU + STFT on GPU", KOKORO_GPU, "cuda", None, "heuristic"))
    runs += [("CPU default", "kokoro-v1.0.onnx", "cpu", None, None),
             ("CPU 8 threads", "kokoro-v1.0.onnx", "cpu", 8, None)]
    best = None
    for label, model_file, device, threads, search in runs:
        if threads and threads > cores:
            continue
        model, voices = kokoro_paths(model_file)
        try:
            session = make_session(model, device, threads, search or "heuristic")
            if device == "cuda" and session.get_providers()[0] != "CUDAExecutionProvider":
                print(f"  {label:<22} failed to start on the GPU")
                continue
            kokoro = Kokoro.from_session(session, str(voices))
            for _ in range(2):
                kokoro.create("Warm up.", voice=k.voice, speed=k.speed, lang=k.lang)
        except Exception as e:
            print(f"  {label:<22} error: {e}")
            continue
        # Unseen sentences (new lengths) are what matters in conversation, so time
        # fresh phrases separately from repeats.
        fresh, repeat = [], []
        for i, p in enumerate(phrases * 2 + [f"Your next meeting is in {n} minutes, sir." for n in range(5, 50, 7)]):
            t = time.perf_counter()
            kokoro.create(p, voice=k.voice, speed=k.speed, lang=k.lang)
            (repeat if 3 <= i < 6 else fresh).append((time.perf_counter() - t) * 1000)
        ms = statistics.median(fresh)
        # Where does the time go? Phonemes (espeak, CPU) vs the neural model.
        ph, model_t = [], []
        for p in phrases:
            t = time.perf_counter()
            phon = kokoro.tokenizer.phonemize(p, k.lang)
            ph.append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            kokoro.create(phon, voice=k.voice, speed=k.speed, lang=k.lang, is_phonemes=True)
            model_t.append((time.perf_counter() - t) * 1000)
        print(f"  {label:<22} {ms:6.0f} ms   (repeat {statistics.median(repeat):.0f} ms; "
              f"phonemes {statistics.median(ph):.0f} + model {statistics.median(model_t):.0f})")
        if best is None or ms < best[0]:
            best = (ms, label, model_file, device, threads, search)

    if not best:
        return
    if profile and gpu:
        prof_model = kokoro_paths(KOKORO_GPU)[0]
        _profile_kokoro(prof_model if prof_model.exists() else kokoro_paths()[0],
                        kokoro_paths()[1], k, phrases)
    ms, label, model_file, device, threads, search = best
    print(f"\nFastest: {label} ({ms:.0f} ms). Put this in config/local.yaml under voice: -> tts:\n")
    print("    kokoro:")
    print(f"      device: {'auto' if device == 'cuda' else 'cpu'}")
    if threads:
        print(f"      threads: {threads}")
    if model_file not in ("kokoro-v1.0.onnx", KOKORO_GPU):
        print(f"      model_file: {model_file}")
    if search and search != "heuristic":
        print(f"      cuda_conv_search: {search}")
    if not gpu:
        print("\nThe GPU wasn't used. To enable it:  "
              "powershell -ExecutionPolicy Bypass -File scripts\\enable_gpu_tts.ps1")


def _profile_kokoro(model, voices, k, phrases) -> None:
    """Which parts of the voice model are slow, and do any run on the CPU instead of the GPU?"""
    import json
    import tempfile
    from collections import defaultdict

    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    from assistant.voice.tts.kokoro import cuda_available

    cuda_available()
    opts = ort.SessionOptions()
    opts.enable_profiling = True
    opts.profile_file_prefix = str(Path(tempfile.gettempdir()) / "kokoro_profile")
    sess = ort.InferenceSession(str(model), sess_options=opts, providers=[
        ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC"}), "CPUExecutionProvider"])
    kokoro = Kokoro.from_session(sess, str(voices))
    kokoro.create("Warm up.", voice=k.voice, speed=k.speed, lang=k.lang)
    for p in phrases:
        kokoro.create(p, voice=k.voice, speed=k.speed, lang=k.lang)
    events = json.loads(Path(sess.end_profiling()).read_text())
    by_provider, by_op = defaultdict(float), defaultdict(float)
    for e in events:
        args = e.get("args", {})
        if e.get("cat") == "Node" and "provider" in args:
            dur = e.get("dur", 0) / 1000 / (len(phrases) + 1)
            by_provider[args["provider"]] += dur
            by_op[(args.get("op_name", "?"), args["provider"].replace("ExecutionProvider", ""))] += dur
    print("\nProfile (average per sentence):")
    for prov, ms in sorted(by_provider.items(), key=lambda x: -x[1]):
        print(f"  {prov:<28} {ms:7.1f} ms")
    print("  slowest operations:")
    for (op, prov), ms in sorted(by_op.items(), key=lambda x: -x[1])[:10]:
        print(f"    {op:<24} {prov:<6} {ms:7.1f} ms")


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="assistant voice")
    p.add_argument("--mode", choices=["wake", "open_mic", "ptt"], help="override voice.mode from config")
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
