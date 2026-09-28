"""What's going on right now, for the model's <context>: which window is in front (where keys
and typing go), what Nova's own browser is showing, and what's playing. Owner: "he doesn't know
what's going on ... he doesn't know when he's selected onto the browser".

Each part is best effort: on a PC without Windows (tests) it just says nothing.
"""

from __future__ import annotations

from urllib.parse import urlparse


def _window() -> str | None:
    try:
        from assistant.tools import pc
        from assistant.tools.keyboard import app_label
        w = pc.WINDOWS.active()
    except Exception:
        return None
    if w is None:
        return None
    return f"{app_label(w)}: {w.title[:90]}"


def _user_browser() -> str | None:
    """The user's own browser when it isn't the window in front ('close this tab' still means it)."""
    try:
        from assistant.tools import mybrowser, pc
        front = pc.WINDOWS.active()
        if mybrowser.is_browser(front):
            return None
        w = mybrowser.find_browser()
    except Exception:
        return None
    return f"{mybrowser.label(w)}: {mybrowser._page_title(w.title)[:80]}"


def _browser() -> str | None:
    from assistant.tools.browser import BROWSER
    page = BROWSER.page
    if page is None or page.is_closed():
        return None
    try:
        where = urlparse(page.url).netloc.removeprefix("www.") or page.url
    except Exception:
        where = ""
    return f"{BROWSER.title or 'a page'} ({where})" if where else (BROWSER.title or "open")


async def situation() -> dict[str, str]:
    import asyncio
    out: dict[str, str] = {}
    w = await asyncio.to_thread(_window)
    if w:
        out["window in front (keys and typing go here)"] = w
    ub = await asyncio.to_thread(_user_browser)
    if ub:
        out["the user's browser (my_browser), behind"] = ub
    b = _browser()
    if b:
        out["your own browser (browser tool) shows"] = b
    try:
        from assistant.hud.server import now_playing_items
        items = await asyncio.wait_for(now_playing_items(), 1.5)
    except Exception:
        items = None
    if items:
        m = items[0]
        out["media"] = f"{m['status']}: {m['title']}" + (f" by {m['artist']}" if m.get("artist") else "") + f" ({m['app']})"
    return out
