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
