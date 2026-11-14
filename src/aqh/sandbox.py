"""OS sandbox for commands that run repository code: Seatbelt on macOS, bubblewrap on Linux, none on Windows.

Writes are allowed only in the workspace, its scratch folder and the temp folder; credential folders under the home
directory are unreadable; network is off unless asked. `wrap` never returns a bare argv: the caller decides whether
to run unsandboxed.
Difference: bubblewrap without network also drops localhost (own network namespace); Seatbelt keeps localhost.
ponytail: network is all or nothing; a per-domain allowlist comes when a team asks for it.
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from aqh.workspace import Workspace

# Under the home directory. ponytail: .npmrc and .pypirc stay readable because package installs need them;
# upgrade: hand the sandbox a token-stripped copy.
SECRET_DIRS = (".ssh", ".aws", ".azure", ".gnupg", ".config/gh", ".config/gcloud", ".kube", ".docker",
               "Library/Keychains")
SECRET_FILES = (".netrc", ".git-credentials")
DOCKER_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock")  # plus ~/.docker/run/docker.sock, ~/.colima, ~/.orbstack
ENV_ALLOW = ("PATH", "HOME", "USERPROFILE", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL", "USER", "LOGNAME")
ENV_ALLOW_WINDOWS = ("SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "WINDIR", "APPDATA", "LOCALAPPDATA",
                     "PROGRAMFILES", "USERNAME", "PROGRAMDATA", "PROGRAMFILES(X86)", "PROCESSOR_ARCHITECTURE",
                     "NUMBER_OF_PROCESSORS", "HOMEDRIVE", "HOMEPATH")


class SandboxUnavailable(RuntimeError):
    """No OS sandbox on this machine."""


def _quote(path: Path) -> str:
    text = str(Path(path).resolve())
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError(f"Path {text!r} has a control character and cannot go into a sandbox profile.")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(writable: list[Path], network: bool) -> str:
    home = Path.home()
    places = " ".join(f"(subpath {_quote(p)})" for p in writable)
    secrets = " ".join([f"(subpath {_quote(home / d)})" for d in SECRET_DIRS]
                       + [f"(literal {_quote(home / f)})" for f in SECRET_FILES])
    docker = " ".join([f"(path-literal {_quote(Path(s))})" for s in (*DOCKER_SOCKETS, home / ".docker/run/docker.sock")]
                      + [f"(subpath {_quote(home / d)})" for d in (".colima", ".orbstack")])
    if network:  # unix sockets stay open except the container engines
        net = f"(deny network-outbound (remote unix-socket {docker}))"
    else:  # this machine's TCP, and unix sockets only inside the writable folders (no ssh-agent, DNS daemon, Docker)
        socks = " ".join(f"(subpath {_quote(p)})" for p in writable)
        net = ('(deny network-outbound)(allow network-outbound (remote ip "localhost:*") '
               f"(remote unix-socket {socks}))")
    # Launch Services and Apple Events would let sandboxed code `open` a file outside the sandbox.
    # ponytail: measured on macOS 27.2: the Apple Event denial takes effect (Finder query fails with -600), but `open`
    # exits 0 under every profile tried, so no test proves Launch Services is cut off. Until one does, commands that
    # run `open` or `osascript` go through approval (F10).
    escape = ('(deny mach-lookup (global-name "com.apple.coreservices.launchservicesd") '
              '(global-name "com.apple.coreservices.appleevents"))(deny appleevent-send)')
    return ("(version 1)(allow default)(deny file-write*)"
            f'(allow file-write* {places} (literal "/dev/null") (literal "/dev/tty") (regex #"^/dev/fd/"))'
            f"(deny file-read* file-write* {secrets})" + escape + net)


def bubblewrap_args(writable: list[Path], network: bool) -> list[str]:
    """--unshare-pid lets --proc mount without privileges and hides the host's /proc (the harness's environ).
    ponytail: other unix sockets (ssh-agent, DNS daemon) stay reachable; upgrade: a seccomp filter or a netns proxy.
    Docker's per-user socket lives in ~/.docker, which is already hidden."""
    home = Path.home()
    binds = [a for p in writable for a in ("--bind", str(Path(p).resolve()), str(Path(p).resolve()))]
    hide = [a for d in SECRET_DIRS if (home / d).is_dir() for a in ("--tmpfs", str(home / d))]
    blank = [str(home / f) for f in SECRET_FILES if (home / f).is_file()]
    blank += sorted({str(Path(s).resolve()) for s in DOCKER_SOCKETS if Path(s).is_socket()})
    hide += [a for f in blank for a in ("--ro-bind", "/dev/null", f)]
    return ["--ro-bind", "/", "/", "--dev", "/dev", "--unshare-pid", "--proc", "/proc", *binds, *hide,
            *([] if network else ["--unshare-net"]), "--die-with-parent", "--new-session"]


def _command(which: str, writable: list[Path], network: bool, argv: list[str]) -> list[str]:
    if which == "seatbelt":
        return [shutil.which("sandbox-exec"), "-p", seatbelt_profile(writable, network), *argv]
    return [shutil.which("bwrap"), *bubblewrap_args(writable, network), "--", *argv]


@functools.cache
def kind() -> str | None:
    """"seatbelt", "bubblewrap", or None. Probes with the same argument shape `wrap` produces."""
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        which = "seatbelt"
    elif sys.platform.startswith("linux") and shutil.which("bwrap"):
        which = "bubblewrap"
    else:
        return None
    true = shutil.which("true") or "/usr/bin/true"
    try:
        probe = _command(which, [Path(tempfile.gettempdir())], False, [true])
        ok = subprocess.run(probe, capture_output=True, timeout=10, env={"PATH": os.environ.get("PATH", "")},
                            check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    return which if ok else None


def wrap(argv: list[str], ws: Workspace, *, network: bool = False) -> list[str]:
    which = kind()
    if which is None:
        if sys.platform == "darwin":
            hint = "sandbox-exec is part of macOS but did not run here."
        elif sys.platform.startswith("linux"):
            hint = "Install bubblewrap (apt install bubblewrap) and allow unprivileged user namespaces."
        else:
            hint = "Windows has no OS sandbox; commands there need approval from a named person."
        raise SandboxUnavailable(f"No OS sandbox on {sys.platform}. {hint}")
    return _command(which, [ws.root, ws.scratch, Path(tempfile.gettempdir())], network, argv)


def clean_env(ws: Workspace, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Allowlisted environment for repository code: nothing of the harness leaks in (no PYTHONPATH, keys, tokens)."""
    allowed = ENV_ALLOW + (ENV_ALLOW_WINDOWS if sys.platform == "win32" else ())
    env = {k: os.environ[k] for k in allowed if k in os.environ}
    if sys.prefix != sys.base_prefix and "PATH" in env:  # the harness's own venv must not be the repository's Python
        mine = os.path.normcase(str(Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin")))
        env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if os.path.normcase(p) != mine)
    env.update(TMPDIR=str(ws.scratch), TMP=str(ws.scratch), TEMP=str(ws.scratch), AQH_PROJECT_DIR=str(ws.root))
    venv = ws.root / ".venv"
    bins = [venv / ("Scripts" if sys.platform == "win32" else "bin"), ws.root / "node_modules" / ".bin"]
    first = [str(d) for d in bins if d.is_dir()]
    if bins[0].is_dir():
        env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = os.pathsep.join(p for p in [*first, env.get("PATH", "")] if p)
    return {**env, **(extra or {})}
