import os
import sys

import pytest

from aqh.workspace import PathRefused, Workspace, windows_problem


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "repo").mkdir()
    return Workspace.open(tmp_path / "repo")


def test_open_needs_an_existing_directory(tmp_path):
    with pytest.raises(ValueError, match="not an existing directory"):
        Workspace.open(tmp_path / "missing")


def test_relative_path_starts_at_root_not_cwd(ws, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ws.resolve("a/b.txt") == ws.root / "a" / "b.txt"
    assert ws.resolve(ws.scratch / "x") == ws.scratch / "x"


def test_paths_outside_are_refused_with_a_fix(ws, tmp_path):
    for bad in ("../x", str(tmp_path / "outside.txt")):
        with pytest.raises(PathRefused) as e:
            ws.resolve(bad)
        assert str(bad) in str(e.value) and f"inside {ws.root}" in str(e.value)


@pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs privileges on Windows")
def test_symlink_pointing_outside_is_refused(ws, tmp_path):
    (tmp_path / "secret").mkdir()
    os.symlink(tmp_path / "secret", ws.root / "link")
    with pytest.raises(PathRefused):
        ws.resolve("link/file")


def test_protected_parts_cannot_be_written_but_can_be_read(ws):
    for p in (".git/config", "node_modules/x", ".VENV/x", "sub/.git/hooks/pre-commit"):
        with pytest.raises(PathRefused) as e:
            ws.resolve(p, for_write=True)
        assert "protected" in str(e.value) and "write somewhere else" in str(e.value)
        assert ws.resolve(p) == (ws.root / p).resolve()
    assert ws.resolve("src/a.py", for_write=True) == ws.root / "src" / "a.py"
    assert ws.resolve(ws.scratch / "t", for_write=True) == ws.scratch / "t"


def test_relative_scratch_is_taken_from_root_not_cwd(ws, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert Workspace.open(ws.root, "tmp/s").scratch == ws.root / "tmp" / "s"


def test_a_null_byte_is_a_refusal_with_a_fix(ws):
    with pytest.raises(PathRefused, match="not a valid path"):
        ws.resolve("a\x00b")


def test_windows_path_forms_are_flagged():
    drives = {"c:"}
    assert windows_problem(r"\\server\share\x", drives) and windows_problem(r"\\?\C:\x", drives)
    assert "drive D:" in windows_problem(r"D:\x", drives)
    assert "current folder" in windows_problem("C:x", drives)
    assert windows_problem(r"C:\repo\a.py", drives) is None and windows_problem("a/b.py", drives) is None
