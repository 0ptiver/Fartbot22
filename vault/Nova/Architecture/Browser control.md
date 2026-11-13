---
tags: [nova, architecture, browser]
---
# Browser control

## Your own browser (Firefox): `tools/mybrowser.py`
- `my_browser` actions: new tab, open in a new tab, go to a tab, close a tab, tab N, reopen, list tabs, find on page...
- Works even when Firefox isn't in front: `find_browser` → `_bring` (checked focus) → act → **verify** via UI Automation tabs, the title or the address bar.
- "Close Gmail" closes the **Gmail tab**, not Firefox (`has_tab`).
- Browser shortcuts always go through `my_browser`, never blind keys to whatever is in front.

## Any app: `tools/apps.py`
`app` tool: look / click / type / press in any app, brought forward first. "Click send in Discord", "in Spotify click shuffle".

## Nova's own browser: `tools/browser.py`
Playwright + installed Edge with its own profile (`data/nova-browser`). Actions: open, search, click, type, select, press, scroll, back, read, look (numbered elements). Fast path `browser_intent`. Can handle `open_website` when `settings.browser.handle_websites` is on.

## Rules learned the hard way
- Keys only reach a web page if the page has the keyboard: prefer clicking the app's own button (found by name via UI Automation), with keys as a backup.
- Never claim a key press worked: checkable combos are verified by a title change; others report "pressed Ctrl+V" rather than "Pasted".

Related: [[Tools and safety]] · [[Screen reading]]
