import re
import sys
import tempfile
import time

import pytest

from aqh import sandbox, shell
from aqh.sandbox import SandboxUnavailable
from aqh.shell import Shell, argv, find_bash, find_powershell, is_benign_exit1, taskkill_argv
from aqh.workspace import Workspace

PY = sys.executable.replace("\\", "/")
FAKE = "sk-" + "FAKE" * 6  # obviously fake
needs_bash = pytest.mark.skipif(find_bash() is None, reason="no bash on this machine")
needs_pwsh = pytest.mark.skipif(find_powershell() is None, reason="no PowerShell on this machine")
needs_sandbox = pytest.mark.skipif(sandbox.kind() is None, reason="no OS sandbox usable here (sandbox.kind() is None)")


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "repo" / "app").mkdir(parents=True)
    (tmp_path / "repo" / "app" / "a.py").write_text("x = 1\nreturn_me = 2\n")
    return Workspace.open(tmp_path / "repo")


@pytest.fixture
def sh(ws):
    s = Shell(ws, sandboxed=False)
    yield s
    s.stop_all()


def wait_for(fn, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if value := fn():
            return value
        time.sleep(0.1)
    return fn()


def dead(sh, pid) -> bool:
    """Through bash, so it also works on Git bash for Windows."""
    return sh.run(f"kill -0 {pid}").exit_code != 0


def start(sh, command, **kw) -> str:
    return re.search(r"bg_[0-9a-f]{6}", sh.start(command, **kw)).group(0)


def log_of(ws, tid):
    return ws.root / ".aqh" / "tasks" / f"{tid}.log"


# ---- pure, all systems ----

def test_argv_shapes(monkeypatch):
    assert argv("bash", "/b/bash", "ls") == ["/b/bash", "--noprofile", "--norc", "-c", "ls"]
    monkeypatch.setattr(sys, "platform", "linux")
    assert argv("powershell", "pwsh", "ls") == ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "ls"]
    monkeypatch.setattr(sys, "platform", "win32")
    assert argv("powershell", "pwsh", "ls") == ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive",
                                                "-ExecutionPolicy", "Bypass", "-Command", "ls"]
    assert taskkill_argv(42) == ["taskkill", "/T", "/F", "/PID", "42"]


def test_windows_bash_is_git_bash_never_wsl(tmp_path, monkeypatch):
    git = tmp_path / "Git" / "bin"
    git.mkdir(parents=True)
    (git / "bash.exe").write_text("")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setattr(shell.shutil, "which", lambda name: r"C:\Windows\System32\bash.exe")
    assert find_bash() == str(git / "bash.exe")
    monkeypatch.setattr(shell.shutil, "which", lambda name: r"D:\tools\Git\bin\bash.exe")
    assert find_bash() == r"D:\tools\Git\bin\bash.exe"
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "none"))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.setattr(shell.shutil, "which", lambda name: r"C:\Windows\System32\bash.exe")
    assert find_bash() is None


def test_benign_exit_one_looks_at_the_last_command():
    for yes in ("grep a b", "FOO=1 BAR=2 grep a b", "make && diff a b", "ls || find . -name x", "cat f; rg x",
                "git diff", "git grep x", "/usr/bin/grep x", "test -f x", "[ -f x ]"):
        assert is_benign_exit1(yes), yes
    for no in ("false", "grep x f | false", "git status", "grep x f | wc -l", "", "npm test"):
        assert not is_benign_exit1(no), no


def test_missing_program_and_unknown_kind_are_sensor_messages(sh, ws, monkeypatch):
    monkeypatch.setattr(shell, "find_bash", lambda: None)
    monkeypatch.setattr(shell, "find_powershell", lambda: None)
    r = sh.run("echo hi")
    assert r.exit_code is None and "No bash on this machine" in r.text and "Nothing ran" in r.text
    assert "No powershell" in sh.run("x", kind="powershell").text
    assert "Unknown shell kind 'zsh'" in sh.run("x", kind="zsh").text
    assert "No bash" in sh.start("sleep 1")
    assert list((ws.root / ".aqh" / "tasks").iterdir()) == []  # no empty log is left behind
    assert list(ws.scratch.iterdir()) == []  # nor scratch output files


def test_sandboxed_run_without_a_sandbox_raises(ws, monkeypatch):
    monkeypatch.setattr(shell, "find_bash", lambda: "/bin/bash")
    monkeypatch.setattr(sandbox, "kind", lambda: None)
    with pytest.raises(SandboxUnavailable):
        Shell(ws, sandboxed=True).run("echo hi")


# ---- bash ----

@needs_bash
def test_every_call_starts_at_the_root_and_nothing_carries(sh, ws):
    first = sh.run("cd app && pwd")
    assert first.text.splitlines()[0].endswith("app") and "exit code 0" in first.text
    assert sh.run("pwd").text.splitlines()[0] == str(ws.root)
    sh.run("export X=1")
    assert "[]" in sh.run('echo "[$X]"').text
    piped = sh.run("grep -c return app/a.py | tr 1 Z")
    assert piped.text.startswith("Z") and "exit code 0" in piped.text


@needs_bash
def test_a_background_child_is_not_a_timeout_and_is_killed(sh, ws):
    began = time.monotonic()
    r = sh.run("sleep 30 & echo $! > p; echo hi")
    assert time.monotonic() - began < 5
    assert not r.timed_out and r.exit_code == 0 and r.text.startswith("hi") and "exit code 0" in r.text
    assert wait_for(lambda: dead(sh, int((ws.root / "p").read_text())), 5), "the stray `sleep 30` outlived the call"


@needs_bash
def test_timeout_kills_the_whole_process_group(sh, ws):
    r = sh.run("sleep 60 & echo $! > pid.txt; sleep 5", timeout_ms=1000)
    assert r.timed_out and r.exit_code is None
    assert "Timed out after 1 s and was stopped" in r.text and "background" in r.text
    assert wait_for(lambda: dead(sh, int((ws.root / "pid.txt").read_text())), 5), "the grandchild survived"


@needs_bash
def test_output_written_before_a_timeout_is_kept(sh):
    r = sh.run("echo before; sleep 5", timeout_ms=1000)
    assert r.timed_out and r.text.startswith("before")


@needs_bash
def test_exit_one_is_benign_for_search_and_a_failure_otherwise(sh):
    assert "exit code 1 (no matches or a difference found; not an error)" in sh.run("grep nothing-here app/a.py").text
    assert "not an error" in sh.run("echo x && FOO=1 grep nothing app/a.py").text
    assert "not an error" not in sh.run("grep nothing app/a.py | false").text
    assert sh.run("exit 3").text.endswith("exit code 3")
    assert sh.run("false").text.endswith("exit code 1")


@needs_bash
def test_long_output_spills_and_aqh_ignores_itself(sh, ws):
    (ws.root / ".git").mkdir()
    r = sh.run(f'{PY} -c "print(\'x\' * 40000)"')
    assert len(r.text) < 3000 and r.spilled and r.spilled.startswith(".aqh/out/") and r.spilled in r.text
    assert (ws.root / r.spilled).read_text().count("x") >= 40000
    assert (ws.root / ".aqh" / ".gitignore").read_text() == "*\n"
    assert not (ws.root / ".git" / "info").exists()  # `.git` is never touched
    sh.run(f'{PY} -c "print(\'y\' * 40000)"')
    assert (ws.root / ".aqh" / ".gitignore").read_text() == "*\n"
    assert list(ws.scratch.iterdir()) == []  # the scratch files are gone; the spill is the only copy


@needs_bash
def test_git_as_a_file_is_left_alone(sh, ws):
    (ws.root / ".git").write_text("gitdir: ../elsewhere\n")  # a worktree or submodule
    r = sh.run(f'{PY} -c "print(\'x\' * 40000)"')
    assert r.spilled and (ws.root / ".git").read_text() == "gitdir: ../elsewhere\n"
    assert (ws.root / ".aqh" / ".gitignore").read_text() == "*\n"


@needs_bash
def test_the_spilled_file_is_redacted_too(sh, ws):
    r = sh.run(f'{PY} -c "print(\'x\' * 40000); print(\'{FAKE}\')"')
    assert r.spilled and FAKE not in (ws.root / r.spilled).read_text()
    assert "sk-F" in (ws.root / r.spilled).read_text()


@needs_bash
def test_stderr_is_labelled_and_secrets_are_redacted(sh):
    r = sh.run(f"echo {FAKE}; echo oops >&2")
    assert FAKE not in r.text and "sk-F" in r.text and "[stderr]\noops" in r.text


@needs_bash
def test_environment_is_clean(sh, monkeypatch):
    monkeypatch.setenv("AQH_FAKE_API_TOKEN", "not-for-the-repository")
    assert "not-for" not in sh.run("env").text


# ---- PowerShell ----

@needs_pwsh
def test_powershell_starts_fresh_and_reports_the_exit_code(sh, ws):
    sh.run("Set-Location app; $env:GREETING = 'hello'", kind="powershell")
    r = sh.run("(Get-Location).Path; $env:GREETING", kind="powershell")
    assert r.text.splitlines()[0].replace("\\", "/").endswith(ws.root.name) and "hello" not in r.text
    assert "exit code 0" in r.text
    assert sh.run("exit 3", kind="powershell").exit_code == 3


# ---- sandbox (needs a real OS sandbox; skipped where sandbox.kind() is None) ----

@pytest.fixture
def narrow_temp(ws, monkeypatch):
    """The sandbox makes the temp folder writable; make it the scratch folder so tmp_path itself is outside."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(ws.scratch))


@needs_bash
@needs_sandbox
def test_sandboxed_run_writes_only_in_the_workspace(ws, tmp_path, narrow_temp):
    outside = tmp_path / "outside.txt"
    s = Shell(ws, sandboxed=True)
    assert "exit code 0" in s.run("echo in > inside.txt && cat inside.txt").text
    assert (ws.root / "inside.txt").read_text().strip() == "in"
    r = s.run(f"echo out > '{outside}'")
    assert not outside.exists() and "exit code 0" not in r.text


@needs_bash
@needs_sandbox
def test_sandboxed_timeout_leaves_no_child_behind(ws, narrow_temp):
    """Under bubblewrap `--new-session` takes the children out of our group; --die-with-parent and the pid
    namespace must end them. A child that survives would write the marker after 3 s."""
    s = Shell(ws, sandboxed=True)
    r = s.run("(sleep 3; echo alive > marker.txt) & sleep 20", timeout_ms=1000)
    assert r.timed_out
    time.sleep(4.5)
    assert not (ws.root / "marker.txt").exists(), "a child of the sandboxed command survived the timeout"


# ---- background tasks ----

SERVER = f'{PY} -u -c "import time; print(\'server up\'); time.sleep(60)"'


def burst(text: str) -> str:
    return f'{PY} -u -c "import time; print({text}); time.sleep(60)"'


@needs_bash
def test_background_task_reports_once_then_stops(sh, ws):
    tid = start(sh, SERVER, description="test server")
    notes = wait_for(sh.updates)
    assert len(notes) == 1 and "server up" in notes[0] and "running" in notes[0]
    assert sh.updates() == []
    assert "ended" in sh.stop(tid)
    assert "server up" in sh.output(tid)
    assert len(sh.updates()) == 1 and sh.updates() == []  # the end, reported once
    assert "No background task nope" in sh.output("nope")


@needs_bash
def test_stop_and_the_time_limit_kill_grandchildren(sh, ws):
    a = start(sh, "sleep 60 & echo $! > kid_a; sleep 60")
    assert wait_for(lambda: (ws.root / "kid_a").exists())
    assert "stopped on request" in sh.stop(a)
    assert wait_for(lambda: dead(sh, int((ws.root / "kid_a").read_text())), 5), "stop() left the grandchild"
    b = start(sh, "sleep 60 & echo $! > kid_b; sleep 60", timeout_ms=1000)
    assert wait_for(lambda: "1-second limit" in sh.output(b))
    assert wait_for(lambda: dead(sh, int((ws.root / "kid_b").read_text())), 5), "the limit left the grandchild"


@needs_bash
def test_children_die_when_the_leader_exits(sh, ws):
    start(sh, "sleep 60 & echo $! > kid_c")
    assert wait_for(lambda: (ws.root / "kid_c").exists())
    assert wait_for(lambda: dead(sh, int((ws.root / "kid_c").read_text())), 5), "the watcher left a stray child"


@needs_bash
def test_a_log_past_the_cap_ends_the_task(sh, ws):
    sh.log_cap = 1000
    tid = start(sh, burst("'q' * 5000"))
    assert wait_for(lambda: "ended with exit code" in sh.output(tid, 200)), sh.output(tid, 200)
    assert "stopped: log passed 1000 bytes" in sh.output(tid, 200)


@needs_bash
def test_output_keeps_the_tail_and_counts_what_it_cut(sh, ws):
    tid = start(sh, burst("'a' * 10000 + 'b' * 500"))
    assert wait_for(lambda: log_of(ws, tid).exists() and log_of(ws, tid).stat().st_size >= 10501)
    text = sh.output(tid, tail_chars=500)
    assert text.startswith("[10001 earlier bytes are in .aqh/tasks/") and "a" * 10 not in text
    assert "b" * 400 in text


@needs_bash
def test_a_burst_of_output_reports_what_was_skipped(sh, ws):
    tid = start(sh, burst("'z' * 9000"))
    assert wait_for(lambda: log_of(ws, tid).exists() and log_of(ws, tid).stat().st_size >= 9001)
    (note,) = sh.updates()
    assert "earlier bytes skipped" in note and f".aqh/tasks/{tid}.log" in note
    assert "z" * 1900 in note and "z" * 2000 not in note
    assert sh.updates() == []


@needs_bash
def test_split_utf8_characters_survive_the_cut(sh, ws):
    tid = start(sh, f'{PY} -u -c "import sys,time; sys.stdout.buffer.write((chr(233)*9000).encode()+bytes([10])); '
                    f'sys.stdout.flush(); time.sleep(60)"')
    assert wait_for(lambda: log_of(ws, tid).exists() and log_of(ws, tid).stat().st_size >= 18001)
    (note,) = sh.updates()
    assert "\ufffd" not in note and "\u00e9" * 1900 in note
    assert "\ufffd" not in sh.output(tid, tail_chars=1001)


@needs_bash
def test_updates_and_output_are_redacted(sh, ws):
    tid = start(sh, burst(f"'{FAKE}'"))
    (note,) = wait_for(sh.updates)
    assert FAKE not in note and "sk-F" in note
    assert FAKE not in sh.output(tid)
