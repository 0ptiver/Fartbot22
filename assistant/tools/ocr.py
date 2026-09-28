"""Nova's own eyes: the text on the screen and where it is, read by Windows' built-in text
recognition (OCR). Owner: "make an easier way for Nova himself to see the screen, instead of
having to use Claude ... give him actual real time access so he can flawlessly do things and
actually see what he is doing".

- Local and instant (a fraction of a second), no graphics memory, nothing leaves the PC.
- Every word comes with its place on the screen, so Nova can click text anywhere (games and web
  pages included, where Windows' list of buttons is empty) and look again to check it worked.
- All WinRT calls run on video.WINRT (its own thread, multithreaded COM) with a time limit, like
  the media list: they must never hang Nova.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from assistant.tools.registry import ToolError

READ_S = 4.0                 # longest wait for one OCR pass
SETTLE_S = 0.5               # after a click, before looking again


@dataclass
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int


@dataclass
class Line:
    text: str
    words: list[Word] = field(default_factory=list)

    @property
    def box(self) -> tuple[int, int, int, int]:
        xs = [w.x for w in self.words] or [0]
        ys = [w.y for w in self.words] or [0]
        x2 = max((w.x + w.w for w in self.words), default=0)
        y2 = max((w.y + w.h for w in self.words), default=0)
        return min(xs), min(ys), x2 - min(xs), y2 - min(ys)


@dataclass
class Hit:
    text: str
    x: int                    # centre, in screen pixels
    y: int
    score: float


# --- the backend (Windows OCR); tests swap in a fake -------------------------------------------------
class OcrBackend:
    """capture() -> (BGRA bytes, width, height, left, top) of a screen area; read() -> lines."""

    def capture(self, area: tuple[int, int, int, int] | None, monitor: int = 1):
        import mss
        from assistant.tools.grid import _dpi_aware
        _dpi_aware()
        factory = getattr(mss, "MSS", None) or mss.mss
        with factory() as sct:
            if area is None:
                if monitor >= len(sct.monitors):
                    raise ToolError(f"There's no screen {monitor}.")
                m = sct.monitors[monitor]
                area = (m["left"], m["top"], m["width"], m["height"])
            left, top, w, h = area
            shot = sct.grab({"left": left, "top": top, "width": w, "height": h})
            return bytes(shot.bgra), shot.width, shot.height, left, top

    def read(self, bgra: bytes, width: int, height: int) -> list[Line]:
        if sys.platform != "win32":
            raise ToolError("Reading the screen only works on Windows.")
        from assistant.tools.video import WINRT
        # A worker thread (tools run in one): wait on Nova's WinRT thread, with a limit.
        loop = WINRT.loop
        if loop is None:
            WINRT._start()
            loop = WINRT.loop
        fut = asyncio.run_coroutine_threadsafe(self._read(bgra, width, height), loop)
        try:
            return fut.result(READ_S)
        except TimeoutError:
            fut.cancel()
            raise ToolError("Windows' text reader didn't answer in time.")

    async def _read(self, bgra: bytes, width: int, height: int) -> list[Line]:
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise ToolError("Windows has no text-recognition language installed "
                            "(Settings > Time & language > Language: add English with 'Optical character recognition').")
        limit = int(OcrEngine.max_image_dimension)
        scale = 1.0
        if max(width, height) > limit:              # very large screens: read at a smaller size
            from PIL import Image
            scale = limit / max(width, height)
            img = Image.frombytes("RGBA", (width, height), bgra).resize(
                (int(width * scale), int(height * scale)))
            bgra, (width, height) = img.tobytes(), img.size
        try:
            bmp = SoftwareBitmap.create_copy_from_buffer(bgra, BitmapPixelFormat.BGRA8, width, height)
        except Exception:                                # bindings that want an IBuffer
            from winrt.windows.storage.streams import DataWriter
            wr = DataWriter()
            wr.write_bytes(bgra)
            bmp = SoftwareBitmap.create_copy_from_buffer(wr.detach_buffer(), BitmapPixelFormat.BGRA8, width, height)
        result = await engine.recognize_async(bmp)
        lines = []
        for ln in result.lines:
            words = []
            for w in ln.words:
                r = w.bounding_rect
                words.append(Word(w.text, int(r.x / scale), int(r.y / scale), int(r.width / scale), int(r.height / scale)))
            lines.append(Line(ln.text, words))
        return lines


OCR = OcrBackend()


# --- reading --------------------------------------------------------------------------------------
def _target(where: str, monitor: int):
    """The area to read: the window in front (default) or a whole screen. Returns (area, label)."""
    from assistant.tools import pc
    if where == "screen":
        return None, f"screen {monitor}"
    w = pc.active_window()
    left, top, width, height = pc.WINDOWS.rect(w.hwnd)
    if width < 20 or height < 20:
        return None, "the screen"
    return (left, top, width, height), w.process.removesuffix(".exe") or w.title


def read_screen(where: str = "window", monitor: int = 1) -> tuple[str, list[Line]]:
    """(label, lines in screen pixels, top to bottom)."""
    area, label = _target(where, monitor)
    bgra, width, height, left, top = OCR.capture(area, monitor)
    lines = OCR.read(bgra, width, height)
    for ln in lines:
        for w in ln.words:
            w.x += left
            w.y += top
    lines.sort(key=lambda ln: (ln.box[1] // 10, ln.box[0]))
    return label, lines


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip()


def find(lines: list[Line], wanted: str) -> list[Hit]:
    """Where `wanted` is on screen: exact words first (their own box, not the whole line), then
    close matches (OCR misreads a letter now and then). Best first."""
    want = _norm(wanted).split()
    if not want:
        return []
    hits: list[Hit] = []
    for ln in lines:
        words = [_norm(w.text) for w in ln.words]
        n = len(want)
        for i in range(len(words) - n + 1):
            chunk = " ".join(words[i:i + n])
            r = 1.0 if chunk == " ".join(want) else SequenceMatcher(None, chunk, " ".join(want)).ratio()
            if r >= 0.8:
                ws = ln.words[i:i + n]
                x0, y0 = min(w.x for w in ws), min(w.y for w in ws)
                x1, y1 = max(w.x + w.w for w in ws), max(w.y + w.h for w in ws)
                hits.append(Hit(" ".join(w.text for w in ws), (x0 + x1) // 2, (y0 + y1) // 2,
                                r + (0.05 if len(words) == n else 0)))   # a line that's just the words: a button
    hits.sort(key=lambda h: -h.score)
    return hits


def as_text(label: str, lines: list[Line], limit: int = 1800) -> str:
    """For the model and for speech: what's written in the window, top to bottom."""
    body = "\n".join(ln.text for ln in lines if ln.text.strip())
    if not body:
        return f"I can't see any text in {label}."
    if len(body) > limit:
        body = body[:limit].rsplit("\n", 1)[0] + "\n…"
    return f"Text in {label}, top to bottom:\n{body}"


def signature(lines: list[Line]) -> str:
    return "\n".join(ln.text for ln in lines)


# --- clicking what's written --------------------------------------------------------------------
def click_text(wanted: str, mouse, action: str = "click", where: str = "window") -> str:
    """Click the text `wanted` where it's shown, then look again: say what really happened."""
    label, lines = read_screen(where)
    hits = find(lines, wanted)
    if not hits and where == "window":
        label, lines = read_screen("screen")
        hits = find(lines, wanted)
    if not hits:
        raise ToolError(f"I can't see '{wanted}' written on the screen.")
    h = hits[0]
    before = signature(lines)
    mouse.move(h.x, h.y)
    time.sleep(0.05)
    mouse.click("right" if action == "right_click" else "left", action == "double_click")
    time.sleep(SETTLE_S)
    try:
        _, after = read_screen(where)
        changed = signature(after) != before
    except ToolError:
        changed = None
    if changed is False:
        return f"I clicked '{h.text}', but nothing on the screen changed."
    return f"Clicked '{h.text}'."
