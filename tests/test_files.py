"""File tools and the folder allowlist."""

import os

import pytest

from assistant.core.paths import PathNotAllowed, allowed_roots, check_path
from assistant.tools import files
from assistant.tools.registry import ToolContext


@pytest.fixture
def home(tmp_path, settings):
    docs, dl = tmp_path / "Documents", tmp_path / "Downloads"
    (docs / "Taxes").mkdir(parents=True)
    dl.mkdir()
    (docs / "Taxes" / "tax return 2025.pdf").write_text("pdf")
    (docs / "shopping list.txt").write_text("milk\neggs\n")
    (docs / ".secret").mkdir()
    (docs / ".secret" / "tax notes.txt").write_text("hidden")
    (docs / "bank passwords.txt").write_text("hunter2")
    (dl / "game setup.exe").write_text("MZ")
    (tmp_path / "outside.txt").write_text("nope")
    settings.safety.allowed_folders = [str(docs), str(dl)]
    settings.safety.notes_folder = str(docs / "Nova Notes")
    return tmp_path


def roots(home):
    return allowed_roots([str(home / "Documents"), str(home / "Downloads")])


def test_allowlist(home):
    r = roots(home)
    assert check_path(home / "Documents" / "shopping list.txt", r).name == "shopping list.txt"
    for bad, why in [(home / "outside.txt", "outside"),
                     (home / "Documents" / ".." / "outside.txt", "outside"),
                     (home / "Documents" / ".secret" / "tax notes.txt", "hidden"),
                     (home / "Documents" / "bank passwords.txt", "password"),
                     ("relative.txt", "full path")]:
        with pytest.raises(PathNotAllowed, match=why):
            check_path(bad, r)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need admin on Windows")
def test_symlink_cannot_escape(home):
    link = home / "Documents" / "sneaky"
    link.symlink_to(home)
    with pytest.raises(PathNotAllowed, match="outside"):
        check_path(link / "outside.txt", roots(home))


def test_find_files(home, settings):
    ctx = ToolContext(settings)
    out = files.find_files({"query": "tax"}, ctx)
    assert "tax return 2025.pdf" in out and "Taxes" in out
    assert "tax notes" not in out                      # hidden folder skipped
    assert "passwords" not in files.find_files({"query": "bank"}, ctx)
    assert "No files" in files.find_files({"query": "zzz"}, ctx)
    only_folders = files.find_files({"query": "tax", "kind": "folder"}, ctx)
    assert "Taxes" in only_folders and ".pdf" not in only_folders


def test_read_and_list(home, settings):
    ctx = ToolContext(settings)
    out = files.read_text_file({"path": str(home / "Documents" / "shopping list.txt")}, ctx)
    assert "milk" in out and "not instructions" in out
    listing = files.read_text_file({"path": str(home / "Documents")}, ctx)
    assert "[dir] Taxes" in listing and ".secret" not in listing and "passwords" not in listing


def test_create_note(home, settings):
    out = files.create_note({"text": "Buy a new mouse", "title": "Shopping/../evil"}, ToolContext(settings))
    notes = list((home / "Documents" / "Nova Notes").glob("*.txt"))
    assert len(notes) == 1 and notes[0].read_text() == "Buy a new mouse"
    assert ".." not in notes[0].name and "Saved a note" in out


async def test_move_and_rename_need_confirmation(home, settings, registry):
    src = home / "Documents" / "shopping list.txt"
    async def no(*a): return False
    async def yes(*a): return True
    res = await registry.execute("move_file", {"path": str(src), "destination": "groceries.txt"},
                                 ToolContext(settings, confirm=no))
    assert res.is_error and src.exists()
    res = await registry.execute("move_file", {"path": str(src), "destination": "groceries.txt"},
                                 ToolContext(settings, confirm=yes))
    assert not res.is_error and (home / "Documents" / "groceries.txt").exists()
    res = await registry.execute("move_file", {"path": str(home / "Documents" / "groceries.txt"),
                                               "destination": str(home / "outside-folder")},
                                 ToolContext(settings, confirm=yes))
    assert res.is_error and "outside" in res.content
    assert registry.describe("move_file", {"path": "C:/x/a.txt", "destination": "b.txt"}) == "move a.txt to b.txt"


async def test_delete_goes_to_recycle_bin(home, settings, registry, monkeypatch):
    settings.phone.pc_control = "off"          # the "phone can't control the PC" setting
    trashed = []
    import send2trash
    monkeypatch.setattr(send2trash, "send2trash", trashed.append)
    async def yes(*a): return True
    target = home / "Documents" / "shopping list.txt"
    res = await registry.execute("delete_file", {"path": str(target)}, ToolContext(settings, confirm=yes))
    assert res.content == "Sent shopping list.txt to the Recycle Bin." and trashed == [str(target)]
    remote = await registry.execute("delete_file", {"path": str(target)},
                                    ToolContext(settings, remote=True, confirm=yes))
    assert remote.is_error                                           # never from a phone


async def test_opening_a_program_asks_first(home, settings, registry, monkeypatch):
    launched = []
    monkeypatch.setattr(files, "_launch", launched.append)
    asked = []
    async def no(tool, args):
        asked.append(args["path"])
        return False
    ctx = ToolContext(settings, confirm=no)
    doc = await registry.execute("open_file", {"path": str(home / "Documents" / "shopping list.txt")}, ctx)
    assert not doc.is_error and asked == []                          # documents: no question
    exe = await registry.execute("open_file", {"path": str(home / "Downloads" / "game setup.exe")}, ctx)
    assert exe.is_error and len(asked) == 1 and len(launched) == 1  # program: asked, declined
