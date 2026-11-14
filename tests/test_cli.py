"""H0: the CLI starts, reports its version, and `doctor` checks real tools and fails honestly when one is missing."""
import pytest

from aqh import __version__, cli


def test_version(capsys):
    with pytest.raises(SystemExit) as done:
        cli.main(["--version"])
    assert done.value.code == 0 and __version__ in capsys.readouterr().out


def test_doctor_reports_every_tool(capsys):
    code = cli.doctor()
    out = capsys.readouterr().out
    assert all(name in out for name in cli.NEEDS)
    assert code == (0 if "FAIL" not in out else 1)


def test_doctor_fails_when_a_tool_is_missing(capsys, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda program: None)  # no node, no git on PATH
    assert cli.doctor() == 1
    out = capsys.readouterr().out
    assert "FAIL node: not found" in out and "FAIL git: not found" in out and "ok   python" in out
