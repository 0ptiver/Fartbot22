"""Nova's own browser (owner: "open kelley blue book kbb.com and search for a car, and it can
effortlessly navigate the browser and search for it"). Driven for real against small local
sites with a headless Chromium; skipped where there's no Chromium."""

import glob
import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from assistant.brain.intents import match_intent
from assistant.tools import browser as B
from assistant.tools.registry import ToolContext, ToolError

CHROME = next(iter(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")), None)
if importlib.util.find_spec("playwright") is None:
    CHROME = None
needs_chrome = pytest.mark.skipif(CHROME is None, reason="no local Chromium/Playwright")

PAGES = {
    "/": """<html><head><title>Kelley Blue Book | Car values</title></head><body>
      <nav><a href="/cars">Cars for Sale</a> <a href="/values">Car Values</a></nav>
      <form action="/search"><input type="search" name="q" placeholder="Search make, model or keyword"></form>
      </body></html>""",
    "/values": """<html><head><title>What's my car worth</title></head><body>
      <label for=y>Year</label><select id=y name=year><option>Year</option><option>2018</option><option>2019</option></select>
      <label for=m>Make</label><select id=m name=make><option>Make</option><option>Honda</option><option>Toyota</option></select>
      <button onclick="document.title='Value: '+y.value+' '+m.value">Get my value</button></body></html>""",
    "/nosearch": """<html><head><title>No search here</title></head><body><p>Just a page.</p></body></html>""",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/search":
            q = parse_qs(u.query).get("q", [""])[0]
            body = f"<html><head><title>{q} results</title></head><body><h1>Results for {q}</h1>" \
                   f"<a href='/car/1'>2019 Honda Civic LX - $18,500</a></body></html>"
        elif u.path == "/best":
            body = "<html><head><title>Best page</title></head><body>Best page for it</body></html>"
        elif u.path == "/car/1":
            body = "<html><head><title>2019 Honda Civic LX</title></head><body><main>Fair purchase price: $18,500. Mileage 42,000.</main></body></html>"
        else:
            body = PAGES.get(u.path, "<html><body>404</body></html>")
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
async def nova_browser(tmp_path, monkeypatch, settings, site):
    monkeypatch.setattr(B, "PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(B, "site_search_url", lambda s, q: f"{site}/best")
    b = B.NovaBrowser()
    b.executable, b.channel, b.headless = CHROME, None, True
    monkeypatch.setattr(B, "BROWSER", b)
    yield ToolContext(settings)
    await b.close()


@needs_chrome
async def test_open_a_site_and_search_it(nova_browser, site):
    out = await B.browser({"action": "search", "site": f"{site}/", "text": "2019 honda civic"}, nova_browser)
    assert "Searched" in out and "2019 honda civic results" in out
    assert "[1] link \"2019 Honda Civic LX - $18,500\"" in out                     # the next step, by number
    out = await B.browser({"action": "click", "target": "1"}, nova_browser)
    assert "Now on 2019 Honda Civic LX" in out
    read = await B.browser({"action": "read"}, nova_browser)
    assert "$18,500" in read and "information only" in read
    back = await B.browser({"action": "back", "details": False}, nova_browser)
    assert back.startswith("Went back. Now on 2019 honda civic results")


@needs_chrome
async def test_dropdowns_and_buttons_by_name(nova_browser, site):
    out = await B.browser({"action": "open", "site": f"{site}/values"}, nova_browser)
    assert 'dropdown "Year" options: Year, 2018, 2019' in out
    await B.browser({"action": "select", "target": "Year", "text": "2019"}, nova_browser)
    await B.browser({"action": "select", "target": "Make", "text": "hon"}, nova_browser)      # close enough
    out = await B.browser({"action": "click", "target": "Get my value", "details": False}, nova_browser)
    assert "Value: 2019 Honda" in out
    with pytest.raises(ToolError, match="isn't one of the choices"):
        await B.browser({"action": "select", "target": "Make", "text": "Ferrari"}, nova_browser)
    with pytest.raises(ToolError, match="can't find"):
        await B.browser({"action": "click", "target": "Buy now"}, nova_browser)


@needs_chrome
async def test_site_without_a_search_box_goes_to_its_best_page(nova_browser, site):
    out = await B.browser({"action": "search", "site": f"{site}/nosearch", "text": "civic"}, nova_browser)
    assert "no search box I could use" in out and "Best page" in out


@needs_chrome
async def test_closed_window_reopens(nova_browser, site):
    await B.browser({"action": "open", "site": f"{site}/"}, nova_browser)
    await B.BROWSER.page.close()
    out = await B.browser({"action": "open", "site": f"{site}/", "details": False}, nova_browser)
    assert out.startswith("Now on Kelley Blue Book")


def test_sites_by_name():
    assert B.resolve("kelley blue book kbb.com") == "https://kbb.com"
    assert B.resolve("Kelley Blue Book") == "https://kbb.com"
    assert B.resolve("kbb.com/cars") == "https://kbb.com/cars"
    assert B.resolve("some random store").startswith("https://duckduckgo.com/?q=%21ducky")
    with pytest.raises(ToolError):
        B.resolve("javascript://alert(1)")


@pytest.mark.parametrize("text,expected", [
    ("open kelley blue book kbb.com and search for a 2019 honda civic",
     {"action": "search", "site": "kelley blue book kbb.com", "text": "2019 honda civic", "details": False}),
    ("search kbb for 2019 honda civic", {"action": "search", "site": "kbb", "text": "2019 honda civic", "details": False}),
    ("go to amazon and search for gaming headphones",
     {"action": "search", "site": "amazon", "text": "gaming headphones", "details": False}),
])
def test_owners_phrases(text, expected):
    assert match_intent(text) == ("browser", expected)


def test_normal_web_and_youtube_searches_stay_as_they_were():
    assert (match_intent("search youtube for lofi") or ("",))[0] != "browser"
    assert (match_intent("search for lofi on google") or ("",))[0] != "browser"


def test_follow_ups_need_novas_browser_in_use():
    assert match_intent("search for a honda civic") is None
    assert match_intent("search for a honda civic", browser_active=True) == \
        ("browser", {"action": "search", "text": "honda civic", "details": False})
    assert match_intent("open discord", browser_active=True)[0] == "open_app"       # not a click on the page


def test_browser_is_blocked_from_a_phone(registry):
    assert registry.effective_risk("browser", remote=True).value == "blocked"


async def test_websites_stay_in_the_normal_browser_where_novas_cant_run(registry, settings):
    """In tests (not Windows, no Chromium configured) open_website keeps using the default browser."""
    assert not B.available(settings)
    settings.browser.handle_websites = True
    opened = []
    from assistant.tools import pc
    orig = pc._open_browser
    pc._open_browser = opened.append
    try:
        res = await registry.execute("open_website", {"site": "kbb.com"}, ToolContext(settings))
    finally:
        pc._open_browser = orig
    assert not res.is_error and opened == ["https://kbb.com"]
