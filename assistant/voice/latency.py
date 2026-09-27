"""Per-turn latency marks and a readable breakdown."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

STAGES = [
    # (label, from mark, to mark)
    ("end-of-speech detect", "speech_end", "eot_detected"),
    ("stt finalize", "eot_detected", "stt_done"),
    ("llm first token", "stt_done", "llm_first_token"),
    ("first chunk ready", "llm_first_token", "first_chunk"),
    ("tts first audio", "first_chunk", "tts_first_audio"),
    ("to speaker", "tts_first_audio", "playback_start"),
]


@dataclass
class LatencyTracker:
    marks: dict[str, float] = field(default_factory=dict)

    def mark(self, name: str, t: float | None = None) -> None:
        self.marks.setdefault(name, time.perf_counter() if t is None else t)

    def ms(self, a: str, b: str) -> float | None:
        if a in self.marks and b in self.marks:
            return max(0, round((self.marks[b] - self.marks[a]) * 1000))
        return None

    def total_ms(self) -> float | None:
        end = "playback_start" if "playback_start" in self.marks else "tts_first_audio"
        return self.ms("speech_end", end)

    def breakdown(self) -> dict[str, float | None]:
        out = {label: self.ms(a, b) for label, a, b in STAGES}
        out["TOTAL (you stop -> it speaks)"] = self.total_ms()
        return out

    def report(self, target_ms: int = 800) -> str:
        lines = ["latency:"]
        for label, v in self.breakdown().items():
            note = "  (transcribed during your pause)" if label == "stt finalize" and self.marks.get("stt_speculative") else ""
            lines.append(f"  {label:32} {'-' if v is None else f'{v:>5.0f} ms'}{note}")
        total = self.total_ms()
        if total is not None:
            lines.append(f"  {'target':32} {target_ms:>5} ms  {'OK' if total <= target_ms else 'OVER'}")
        return "\n".join(lines)
