import subprocess
import os
import socket
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

from aqh import sandbox
from aqh.sandbox import SandboxUnavailable, clean_env, seatbelt_profile, wrap
from aqh.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "repo").mkdir()
    return Workspace.open(tmp_path / "repo")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".ssh").mkdir(parents=True)
    (h / ".ssh" / "id").write_text("PRIVATE")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


def q(path):  # the same escaping the profile uses, so the expected text is right on Windows too
    return '"' + str(Path(path).resolve()).replace("\\", "\\\\").replace('"', '\\"') + '"'


def test_seatbelt_profile_text(home, tmp_path):
    on, off = seatbelt_profile([tmp_path], True), seatbelt_profile([tmp_path], False)
    assert f"(subpath {q(tmp_path)})" in on
    assert f"(deny file-read* file-write* (subpath {q(home / '.ssh')})" in on
    assert f"(literal {q(home / '.netrc')})" in on and q(home / "Library/Keychains") in on and q(home / ".config/gcloud") in on
    assert "(deny network-outbound (remote unix-socket" in on and "docker.sock" in on and ".colima" in on
    assert "(deny network-outbound)" not in on
    assert '(deny network-outbound)(allow network-outbound (remote ip "localhost:*") ' in off
    assert f"(remote unix-socket (subpath {q(tmp_path)}))" in off
    assert "launchservicesd" in off and "(deny appleevent-send)" in off


def test_profile_escapes_quotes_and_rejects_control_characters(tmp_path):
    assert q(tmp_path / 'a"b') in seatbelt_profile([tmp_path / 'a"b'], True)
    with pytest.raises(ValueError, match="control character"):
        seatbelt_profile([tmp_path / "a\nb"], True)


def test_bubblewrap_arguments(home, tmp_path):
    (home / ".netrc").write_text("x")
    args = sandbox.bubblewrap_args([tmp_path], False)
    here = str(tmp_path.resolve())
    assert args[:8] == ["--ro-bind", "/", "/", "--dev", "/dev", "--unshare-pid", "--proc", "/proc"]
    assert ["--bind", here, here] == args[args.index("--bind"):args.index("--bind") + 3]
    assert ["--tmpfs", str(home / ".ssh")] == args[args.index("--tmpfs"):args.index("--tmpfs") + 2]
    assert ["--ro-bind", "/dev/null", str(home / ".netrc")] == args[-6:-3]
    assert args[-3:] == ["--unshare-net", "--die-with-parent", "--new-session"]
    assert "--unshare-net" in args and "--unshare-net" not in sandbox.bubblewrap_args([tmp_path], True)


@pytest.mark.parametrize("platform, hint", [("linux", "bubblewrap"), ("win32", "approval"), ("darwin", "sandbox-exec")])
def test_wrap_without_a_sandbox_says_what_to_install(ws, monkeypatch, platform, hint):
    monkeypatch.setattr(sandbox, "kind", lambda: None)
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(SandboxUnavailable, match=hint):
        wrap(["true"], ws)


def test_clean_env_drops_the_harness_venv_from_path(ws, tmp_path, monkeypatch):
    venv = tmp_path / "harness-venv"
    mine = venv / ("Scripts" if sys.platform == "win32" else "bin")
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.setenv("PATH", os.pathsep.join([str(mine), str(tmp_path / "other")]))
    env = clean_env(ws)
    assert env["PATH"] == str(tmp_path / "other")  # no harness venv, no empty entries


def test_clean_env(ws, monkeypatch):
    for k in ("PYTHONPATH", "OPENAI_API_KEY", "AQH_TEST_TOKEN"):
        monkeypatch.setenv(k, "leak")
    bins = [ws.root / ".venv" / ("Scripts" if sys.platform == "win32" else "bin"), ws.root / "node_modules" / ".bin"]
    for b in bins:
        b.mkdir(parents=True)
    env = clean_env(ws, {"EXTRA": "1"})
    assert not {"PYTHONPATH", "OPENAI_API_KEY", "AQH_TEST_TOKEN"} & set(env)
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == str(ws.scratch)
    assert env["AQH_PROJECT_DIR"] == str(ws.root) and env["EXTRA"] == "1"
    assert env["VIRTUAL_ENV"] == str(ws.root / ".venv")
    assert env["PATH"].split(os.pathsep)[:2] == [str(b) for b in bins]


needs_sandbox = pytest.mark.skipif(
    sandbox.kind() is None,
    reason=f"no usable OS sandbox on {sys.platform}: needs sandbox-exec (macOS) or bubblewrap with user namespaces (Linux)")


def run_argv(ws, argv, *, network=False):
    return subprocess.run(wrap(argv, ws, network=network), capture_output=True, text=True, timeout=30,
                          env=clean_env(ws), check=False)


def run(ws, code, *, network=False):
    return run_argv(ws, [sys.executable, "-c", code], network=network)


@needs_sandbox
def test_real_sandbox_writes_only_in_workspace_and_temp(ws, tmp_path, monkeypatch):
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    inside, outside = ws.root / "in.txt", tmp_path / "outside.txt"
    assert run(ws, f"open({str(inside)!r}, 'w').write('x')").returncode == 0 and inside.read_text() == "x"
    assert run(ws, f"open({str(outside)!r}, 'w').write('x')").returncode != 0 and not outside.exists()


@needs_sandbox
def test_real_sandbox_hides_credential_folders(ws, home):
    r = run(ws, f"print(open({str(home / '.ssh' / 'id')!r}).read())")
    assert r.returncode != 0 and "PRIVATE" not in r.stdout


@needs_sandbox
def test_real_sandbox_network_off_and_on(ws):
    code = "import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)"
    off = run(ws, code)
    assert off.returncode != 0 and "timed out" not in off.stderr
    on = run(ws, code, network=True)  # may fail offline, but never because of the sandbox
    assert "not permitted" not in on.stderr.lower() and "PermissionError" not in on.stderr


seatbelt_only = pytest.mark.skipif(sandbox.kind() != "seatbelt", reason=f"needs Seatbelt (macOS); this is {sys.platform}")


@seatbelt_only
def test_real_seatbelt_unix_sockets_only_inside_the_writable_folders(monkeypatch):
    # /tmp keeps socket paths under the 104-byte limit that pytest's tmp_path would exceed
    base = Path(tempfile.mkdtemp(dir="/tmp")).resolve()
    inside_dir, outside_dir = base / "ws", base / "outside"
    inside_dir.mkdir(), outside_dir.mkdir()
    ws = Workspace.open(inside_dir, inside_dir / "s")
    monkeypatch.setattr(tempfile, "tempdir", str(inside_dir / "s"))  # keep /tmp itself outside the writable set
    servers = []
    try:
        for d in (inside_dir, outside_dir):
            srv = socket.socket(socket.AF_UNIX)
            srv.bind(str(d / "k.sock"))
            srv.listen()
            servers.append(srv)
        connect = "import socket; socket.socket(socket.AF_UNIX).connect({!r})"
        assert run(ws, connect.format(str(inside_dir / "k.sock"))).returncode == 0
        assert run(ws, connect.format(str(outside_dir / "k.sock"))).returncode != 0
    finally:
        for srv in servers:
            srv.close()
        shutil.rmtree(base, ignore_errors=True)
