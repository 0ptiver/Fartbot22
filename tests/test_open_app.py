"""Opening apps by the name the owner says: Steam games (.url shortcuts), desktop shortcuts,
Store apps, and "Opened" only once the window is really there."""

import pytest

from assistant.tools import pc, system
from assistant.tools.registry import ToolContext, ToolError


class Wins:
    def __init__(self, wins=()):
        self.wins = list(wins)

    def list(self):
        return self.wins


@pytest.fixture
def start_menu(tmp_path):
    sm, desk = tmp_path / "Start Menu", tmp_path / "Desktop"
    (sm / "Steam").mkdir(parents=True)
    desk.mkdir()
    (sm / "Steam" / "Steam.lnk").write_text("")
    (sm / "Steam" / "Uninstall Steam.lnk").write_text("")
    (desk / "Grand Theft Auto V.url").write_text("[InternetShortcut]\nURL=steam://rungameid/271590")
    (desk / "FiveM.lnk").write_text("")
    (sm / "Rockstar Games Launcher.lnk").write_text("")
    return [sm, desk]


@pytest.mark.parametrize("said,found", [("steam", "Steam"), ("gta", "Grand Theft Auto V"),
                                        ("grand theft auto", "Grand Theft Auto V"), ("five m", "FiveM"),
                                        ("fivem", "FiveM"), ("rockstar", "Rockstar Games Launcher")])
def test_finds_what_the_owner_says(start_menu, said, found):
    assert system.find_shortcut(said, start_menu).stem == found


def test_nothing_like_it(start_menu):
    assert system.find_shortcut("photoshop", start_menu) is None


def test_steam_game_opens_and_is_checked(settings, start_menu, monkeypatch):
    wins = Wins([pc.Win(1, "Discord", "Discord.exe")])
    monkeypatch.setattr(pc, "WINDOWS", wins)
    opened = []

    def launcher(target):
        opened.append(target)
        wins.wins.append(pc.Win(2, "Grand Theft Auto V", "GTA5.exe"))
    out = system.open_app({"name": "gta"}, ToolContext(settings), _launcher=launcher, _dirs=start_menu, _apps=[])
    assert out == "Opened Grand Theft Auto V." and opened[0].endswith("Grand Theft Auto V.url")


def test_no_window_yet_is_not_called_opened(settings, start_menu, monkeypatch):
    """Owner: Nova says it did things it didn't. A big game can take a minute to show."""
    monkeypatch.setattr(pc, "WINDOWS", Wins())
    out = system.open_app({"name": "gta"}, ToolContext(settings), _launcher=lambda t: None, _dirs=start_menu, _apps=[])
    assert out == "Starting Grand Theft Auto V. It can take a moment to appear."


def test_already_open(settings, start_menu, monkeypatch):
    monkeypatch.setattr(pc, "WINDOWS", Wins([pc.Win(5, "Steam", "steamwebhelper.exe")]))
    out = system.open_app({"name": "steam"}, ToolContext(settings), _launcher=lambda t: None, _dirs=start_menu, _apps=[])
    assert out == "Steam is already open."


def test_store_apps_are_found_too(settings, start_menu, monkeypatch):
    monkeypatch.setattr(pc, "WINDOWS", Wins())
    opened = []
    apps = [("Xbox", "Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App"), ("WhatsApp", "5319275A.WhatsAppDesktop!App")]
    system.open_app({"name": "xbox"}, ToolContext(settings), _launcher=opened.append, _dirs=start_menu, _apps=apps)
    assert opened == ["shell:AppsFolder\\Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App"]
    with pytest.raises(ToolError, match="couldn't find an app called 'photoshop'"):
        system.open_app({"name": "photoshop"}, ToolContext(settings), _launcher=opened.append, _dirs=start_menu, _apps=apps)


def test_play_a_game_opens_it(start_menu, monkeypatch):
    from assistant.brain.intents import match_intent
    monkeypatch.setattr(system, "_start_menu_dirs", lambda: start_menu)
    monkeypatch.setattr(system, "_GAMES_CACHE", (0.0, []))
    assert match_intent("play gta") == ("open_app", {"name": "gta"})
    assert match_intent("play grand theft auto") == ("open_app", {"name": "grand theft auto"})
    assert match_intent("play fivem") == ("open_app", {"name": "fivem"})
    assert match_intent("play drake")[0] == "play_music"
    assert match_intent("play gta on spotify")[0] == "play_music"            # asked for the music


class ClosingWins(Wins):
    """WM_CLOSE removes the window, unless the app asks to save first."""

    def __init__(self, wins, asks=False):
        super().__init__(wins)
        self.asks, self.closed = asks, []

    def show(self, hwnd, how):
        if how == "close":
            self.closed.append(hwnd)
            if not self.asks:
                self.wins = [w for w in self.wins if w.hwnd != hwnd]


def test_close_is_checked(settings, monkeypatch):
    wins = ClosingWins([pc.Win(3, "Untitled - Notepad", "notepad.exe")])
    monkeypatch.setattr(pc, "WINDOWS", wins)
    assert pc.window_control({"action": "close", "app": "notepad"}, ToolContext(settings)) == "Closed notepad."
    wins.wins, wins.asks = [pc.Win(3, "*notes - Notepad", "notepad.exe")], True
    out = pc.window_control({"action": "close", "app": "notepad"}, ToolContext(settings))
    assert out.startswith("notepad didn't close") and "save" in out


def test_closing_spotify_says_it_still_runs(settings, monkeypatch):
    """'Closed Spotify' while the music carries on would be another claim that isn't true."""
    monkeypatch.setattr(pc, "WINDOWS", ClosingWins([pc.Win(4, "Spotify Premium", "Spotify.exe")]))
    out = pc.window_control({"action": "close", "app": "spotify"}, ToolContext(settings))
    assert "still running in the tray" in out and 'quit Spotify' in out


def test_quit_ends_a_tray_app(settings, monkeypatch):
    monkeypatch.setattr(pc, "WINDOWS", ClosingWins([pc.Win(4, "Spotify Premium", "Spotify.exe")]))
    ended = []
    monkeypatch.setattr(pc, "end_processes", lambda exe: ended.append(exe) or 3)
    assert pc.window_control({"action": "quit", "app": "spotify"}, ToolContext(settings)) == "Quit Spotify completely."
    assert ended == ["Spotify.exe"]


def test_quit_never_kills_an_app_with_work_in_it(settings, monkeypatch):
    monkeypatch.setattr(pc, "WINDOWS", ClosingWins([pc.Win(3, "notes - Notepad", "notepad.exe")]))
    monkeypatch.setattr(pc, "end_processes", lambda exe: pytest.fail("killed notepad"))
    assert pc.window_control({"action": "quit", "app": "notepad"}, ToolContext(settings)) == "Closed notepad."


@pytest.mark.parametrize("said,action", [("quit spotify", "quit"), ("close spotify completely", "quit"),
                                         ("kill discord", "quit"), ("close spotify", "close")])
def test_quit_phrases(said, action):
    from assistant.brain.intents import match_intent
    assert match_intent(said)[1]["action"] == action


class FocusWins(Wins):
    def __init__(self, wins):
        super().__init__(wins)
        self.focused = []

    def focus(self, hwnd):
        self.focused.append(hwnd)
        return True


def test_back_to_the_game(settings, monkeypatch):
    """Tabbed out of GTA RP to ask something: 'go back to the game'."""
    wins = FocusWins([pc.Win(1, "Nova", "msedge.exe"), pc.Win(2, "YouTube - Mozilla Firefox", "firefox.exe"),
                      pc.Win(3, "Discord", "Discord.exe"), pc.Win(4, "FiveM® by Cfx.re", "FiveM_b2944_GTAProcess.exe")])
    monkeypatch.setattr(pc, "WINDOWS", wins)
    monkeypatch.setattr(pc, "game_processes", lambda: {"fivem_b2944_gtaprocess.exe"})
    out = pc.window_control({"action": "focus", "app": "the game"}, ToolContext(settings))
    assert out == "Switched to FiveM." and wins.focused == [4]
    out = pc.window_control({"action": "focus", "app": "back"}, ToolContext(settings))
    assert out == "Switched to Discord." and wins.focused[-1] == 3        # the window before Firefox
    monkeypatch.setattr(pc, "game_processes", lambda: set())
    with pytest.raises(ToolError, match="can't see a game"):
        pc.window_control({"action": "focus", "app": "the game"}, ToolContext(settings))


class StateWins(Wins):
    def __init__(self, wins, obeys=True):
        super().__init__(wins)
        self.obeys, self.states = obeys, {w.hwnd: "normal" for w in wins}

    def show(self, hwnd, how):
        if self.obeys:
            self.states[hwnd] = {"minimize": "minimized", "maximize": "maximized", "restore": "normal"}[how]

    def state(self, hwnd):
        return self.states[hwnd]


def test_minimise_is_checked(settings, monkeypatch):
    monkeypatch.setattr(pc, "WINDOWS", StateWins([pc.Win(4, "Discord", "Discord.exe")]))
    assert pc.window_control({"action": "minimize", "app": "discord"}, ToolContext(settings)) == "Minimized Discord."
    monkeypatch.setattr(pc, "WINDOWS", StateWins([pc.Win(4, "Discord", "Discord.exe")], obeys=False))
    with pytest.raises(ToolError, match="didn't"):
        pc.window_control({"action": "maximize", "app": "discord"}, ToolContext(settings))
