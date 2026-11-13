---
tags: [nova, architecture]
---
# Tray and autostart

- `assistant/background.py`: the **supervisor** (`python -m assistant background`). It starts `pythonw -m assistant voice --background`, restarts it if it crashes (waiting longer each time, e.g. while Ollama loads after boot), and writes everything to `data\logs\nova.log`. Single-instance mutex: only one Nova at a time.
- `assistant/tray.py`: pystray icon in the state colour (blue listening, purple thinking, grey standing down, red mic off). Right-click: Open / Microphone off / Stand down / Start with Windows / Open log / Restart / Quit.
- Autostart: HKCU Run key, `python -m assistant autostart on|off|status`.

> [!tip] Started from PowerShell it looks frozen
> `python -m assistant background` keeps the window blank (output goes to the log), and closing the window stops Nova. Use `pythonw` instead to start it without a window. See [[Start, stop, update]].
