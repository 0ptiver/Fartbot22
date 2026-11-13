---
tags: [nova, architecture, screen]
---
# Screen reading (Nova's own eyes)

`assistant/tools/ocr.py`: Windows' built-in OCR (WinRT `Windows.Media.Ocr`), so Nova sees the screen itself, in real time, with no Claude needed.

- `read_screen`: the window in front or a whole screen, with words in screen pixels (~380 ms; doctor showed 117 lines).
- `find`: exact match, then fuzzy. `click_text`: clicks, looks again, says if nothing changed.
- `click_element` falls back to OCR when UI Automation lists nothing.
- `look_at_screen` returns the text; the picture goes to Claude only for visual questions or when there's little text.
- All WinRT work runs on `video.WINRT`: its own thread and loop, COM multithreaded, every call time-limited.
- The backend `OCR` is swappable; tests fake it (`tests/test_ocr.py`).

Related: [[Screen watch and windows]] · [[Browser control]]
