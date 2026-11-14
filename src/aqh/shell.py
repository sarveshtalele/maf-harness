"""The one place repository commands run: bash or PowerShell, a fresh process at the workspace root per call.

No persistent shell: `cd` and exported variables never carry to the next call. Every call has a timeout and is
killed as a whole process group; output is redacted, long output is spilled to `.aqh/out/`; background tasks log to
`.aqh/tasks/`. Later tools call `Shell`; nothing else spawns repository commands.
Ported from the legacy shells.py and bg_tasks.py (read-only reference), with its defects fixed: no stale sessions,
`.aqh/.gitignore` instead of editing `.git`, grandchildren die with the group, skipped output is reported.
Output goes to files, never pipes: a backgrounded child that holds a pipe open would look like a timeout.
ponytail: the timeout kill uses SIGTERM then SIGKILL on the group; a process that calls setsid escapes it.
ponytail: a spilled file is redacted line by line, so one enormous line is held in memory.
"""
from __future__ import annotations

import atexit
import codecs
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path

from aqh.redact import redact
from aqh.sandbox import clean_env, wrap
from aqh.workspace import Workspace

DEFAULT_MS, MIN_MS, MAX_MS = 120_000, 1_000, 600_000
TASK_DEFAULT_S, TASK_MAX_S = 30 * 60, 2 * 60 * 60
INLINE, PREVIEW, UPDATE_CHARS = 30_000, 2_000, 2_000
BENIGN_EXIT1 = frozenset({"grep", "rg", "egrep", "fgrep", "find", "diff", "test", "["})
LOG_CAP = 50 << 20  # a background log past this stops its task
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _windows() -> bool:
    return sys.platform == "win32"


def find_bash() -> str | None:
    """On Windows only Git for Windows' bash: `System32\\bash.exe` is WSL, a different machine."""
    found = shutil.which("bash")
    if not _windows():
        return found
    if found and "\\system32\\" not in found.replace("/", "\\").casefold():
        return found
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        candidate = Path(os.environ.get(var, "")) / "Git" / "bin" / "bash.exe"
        if os.environ.get(var) and candidate.is_file():
            return str(candidate)
    return None


def find_powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def argv(kind: str, program: str, command: str) -> list[str]:
    if kind == "bash":
        return [program, "--noprofile", "--norc", "-c", command]
    policy = ["-ExecutionPolicy", "Bypass"] if _windows() else []
    return [program, "-NoLogo", "-NoProfile", "-NonInteractive", *policy, "-Command", command]


def taskkill_argv(pid: int) -> list[str]:
    return ["taskkill", "/T", "/F", "/PID", str(pid)]


def kill_group(proc: subprocess.Popen) -> None:
    """Stop the process and everything it started. Never raises.
    ponytail: on Windows `taskkill /T` cannot reach a tree whose leader already exited; upgrade: a job object."""
    try:
        if _windows():
            subprocess.run(taskkill_argv(proc.pid), capture_output=True, timeout=10, check=False)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                pass
            os.killpg(proc.pid, signal.SIGKILL)  # also reaps grandchildren the leader left behind
        proc.wait(5)
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
        pass


def _group_flags() -> dict:
    if _windows():
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _plain(text: str) -> str:
    return redact(ANSI.sub("", text))


def _span(seconds: float) -> str:
    return f"{seconds / 60:g}-minute" if seconds % 60 == 0 else f"{seconds:g}-second"


def last_program(command: str) -> list[str]:
    """Words of the last simple command (after && || ; |), without leading VAR=x assignments."""
    words = (re.split(r"&&|\|\||;|\||\n", command)[-1:] or [""])[0].split()
    while words and re.fullmatch(r"\w+=\S*", words[0]):
        words.pop(0)
    return words


def is_benign_exit1(command: str) -> bool:
    words = last_program(command)
    if not words:
        return False
    program = words[0].replace("\\", "/").rsplit("/", 1)[-1]
    return program in BENIGN_EXIT1 or (program == "git" and words[1:2] in (["diff"], ["grep"]))


@dataclass
class ShellResult:
    exit_code: int | None
    timed_out: bool
    text: str  # what the model reads
    duration_ms: int
    spilled: str | None = None  # path of the full output, relative to the root, when it was cut


@dataclass
class Task:
    id: str
    command: str
    description: str
    log: Path
    proc: subprocess.Popen
    started: float
    limit_s: float
    seen: int = 0
    ended_reported: bool = False
    note: str = ""


class Shell:
    def __init__(self, ws: Workspace, *, sandboxed: bool, network: bool = False):
        self.ws, self.sandboxed, self.network = ws, sandboxed, network
        self.log_cap = LOG_CAP
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        me = weakref.ref(self)
        atexit.register(lambda: me() and me().stop_all())  # background tasks must not outlive the harness

    def _spawn(self, command: str, kind: str, **io) -> subprocess.Popen | str:
        """The process, or a message for the model. SandboxUnavailable propagates: never a silent unsandboxed run."""
        if kind not in ("bash", "powershell"):
            return f"Unknown shell kind {kind!r}. Use kind=bash or kind=powershell."
        program = find_bash() if kind == "bash" else find_powershell()
        if not program:
            fix = ("on Windows install Git for Windows, or use kind=powershell" if _windows() and kind == "bash"
                   else "use kind=bash" if kind == "powershell" else "install bash")
            return f"No {kind} on this machine; {fix}. Nothing ran."
        cmd = argv(kind, program, command)
        if self.sandboxed:
            cmd = wrap(cmd, self.ws, network=self.network)
        try:
            return subprocess.Popen(cmd, cwd=self.ws.root, env=clean_env(self.ws), stdin=subprocess.DEVNULL,
                                    **io, **_group_flags())
        except OSError as e:
            return f"Could not start {kind} ({e}). Check that it runs from a terminal. Nothing ran."

    def _ignore(self) -> Path:
        """`.aqh/` ignores itself for git, whatever the repository layout; `.git` is never touched."""
        folder = self.ws.root / ".aqh"
        folder.mkdir(exist_ok=True)
        if not (folder / ".gitignore").exists():
            (folder / ".gitignore").write_text("*\n", encoding="utf-8")
        return folder

    def run(self, command: str, *, kind: str = "bash", timeout_ms: int | None = None) -> ShellResult:
        seconds = min(max(timeout_ms or DEFAULT_MS, MIN_MS), MAX_MS) / 1000
        began = time.monotonic()
        self._ignore()
        stem = self.ws.scratch / secrets.token_hex(4)
        out_p, err_p = stem.with_suffix(".out"), stem.with_suffix(".err")
        try:
            with open(out_p, "wb") as fo, open(err_p, "wb") as fe:
                proc = self._spawn(command, kind, stdout=fo, stderr=fe)
            if isinstance(proc, str):
                return ShellResult(None, False, proc, 0)
            timed_out = False
            try:
                proc.wait(seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                kill_group(proc)  # on timeout, and strays started with `&` after a normal exit
            code = None if timed_out else proc.returncode
            return self._render(command, out_p, err_p, code, timed_out, seconds, int((time.monotonic() - began) * 1000))
        finally:
            out_p.unlink(missing_ok=True)
            err_p.unlink(missing_ok=True)

    def _render(self, command, out_p, err_p, code, timed_out, seconds, ms) -> ShellResult:
        size = out_p.stat().st_size + err_p.stat().st_size
        spilled = None
        if size > INLINE:
            spilled = self._spill(out_p, err_p)
            with open(self.ws.root / spilled, encoding="utf-8", errors="replace") as f:
                head = f.read(PREVIEW)
            body = (f"{head}\n\n[harness] Output was {size} bytes; all of it is saved in {spilled}. "
                    "Read part of it or search it instead of printing it again.")
        else:
            out, err = (_plain(p.read_bytes().decode("utf-8", "replace")) for p in (out_p, err_p))
            body = out + (f"\n[stderr]\n{err}" if err.strip() else "")
        if timed_out:
            status = (f"Timed out after {seconds:g} s and was stopped. For a server or a long job, run it in the "
                      f"background, or use a larger timeout_ms (up to {MAX_MS}).")
        elif code == 1 and is_benign_exit1(command):
            status = "exit code 1 (no matches or a difference found; not an error)"
        else:
            status = f"exit code {code}"
        return ShellResult(code, timed_out, f"{body.rstrip()}\n\n{status}".lstrip(), ms, spilled)

    def _spill(self, out_p: Path, err_p: Path) -> str:
        """The full output, redacted line by line, in `.aqh/out/`."""
        folder = self._ignore() / "out"
        folder.mkdir(exist_ok=True)
        path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.txt"
        with open(path, "w", encoding="utf-8", newline="") as w:
            for src, label in ((out_p, ""), (err_p, "\n[stderr]\n")):
                if src.stat().st_size:
                    w.write(label)
                    with open(src, encoding="utf-8", errors="replace", newline="") as f:
                        for line in f:
                            w.write(_plain(line))
        return path.relative_to(self.ws.root).as_posix()

    # ---- background tasks ----

    def start(self, command: str, *, kind: str = "bash", description: str = "", timeout_ms: int | None = None) -> str:
        folder = self._ignore() / "tasks"
        folder.mkdir(exist_ok=True)
        task_id = f"bg_{secrets.token_hex(3)}"
        log = folder / f"{task_id}.log"
        limit = min(max((timeout_ms or TASK_DEFAULT_S * 1000) / 1000, 1), TASK_MAX_S)
        with open(log, "wb") as f:
            proc = self._spawn(command, kind, stdout=f, stderr=subprocess.STDOUT)
        if isinstance(proc, str):
            log.unlink(missing_ok=True)
            return proc
        task = Task(task_id, command, description, log, proc, time.monotonic(), limit)
        with self._lock:
            self._tasks[task_id] = task
        threading.Thread(target=self._watch, args=(task,), daemon=True).start()
        return (f"Started background task {task_id} ({description or command[:80]}). Its output goes to {self._rel(log)}. "
                f"It stops by itself after {_span(limit)}. Read it with output('{task_id}'); stop it with "
                f"stop('{task_id}'). New output and its end are shown to you later.")

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.ws.root).as_posix()

    def _watch(self, task: Task) -> None:
        """Ends the task at its time limit or when its log passes the cap; always kills what the leader left behind."""
        deadline = task.started + task.limit_s
        try:
            while task.proc.poll() is None:
                if time.monotonic() >= deadline:
                    task.note = f"stopped after its {_span(task.limit_s)} limit"
                    break
                if task.log.stat().st_size > self.log_cap:
                    cap = f"{self.log_cap >> 20} MB" if self.log_cap >= 1 << 20 else f"{self.log_cap} bytes"
                    task.note = f"stopped: log passed {cap}"
                    break
                try:
                    task.proc.wait(0.2)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            kill_group(task.proc)

    @staticmethod
    def _status(task: Task) -> str:
        code = task.proc.poll()
        text = (f"running for {int(time.monotonic() - task.started)} s" if code is None
                else f"ended with exit code {code}")
        return f"{text} ({task.note})" if task.note else text

    def _task(self, task_id: str) -> Task | str:
        task = self._tasks.get(task_id)
        return task or f"No background task {task_id}. Known: {', '.join(self._tasks) or 'none'}."

    def _read_new(self, task: Task, chars: int, *, from_start: bool = False) -> tuple[str, int]:
        """At most `chars` characters of the log's newest text, and how many bytes were skipped before it.
        Reads from the offset already seen (or the start), but never more than the last `chars * 4` bytes; the
        offset advances over complete characters only, so a split character is read whole next time."""
        size = task.log.stat().st_size
        first = 0 if from_start else task.seen
        start = max(first, size - chars * 4)
        with open(task.log, "rb") as f:
            f.seek(start)
            data = f.read(size - start)
        if start > first:  # landed inside a character: drop its continuation bytes
            lead = len(data) - len(data.lstrip(bytes(range(0x80, 0xC0))))
            data, start = data[lead:], start + lead
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        text = decoder.decode(data)
        task.seen = start + len(data) - len(decoder.getstate()[0])
        skipped = start - first
        if len(text) > chars:
            skipped += len(text[:-chars].encode())
            text = text[-chars:]
        return _plain(text), skipped

    def output(self, task_id: str, tail_chars: int = 8000) -> str:
        with self._lock:
            task = self._task(task_id)
            if isinstance(task, str):
                return task
            text, skipped = self._read_new(task, tail_chars, from_start=True)
            status = self._status(task)
        cut = f"[{skipped} earlier bytes are in {self._rel(task.log)}]\n" if skipped else ""
        return f"{cut}{text}\n\n{status}"

    def stop(self, task_id: str) -> str:
        with self._lock:
            task = self._task(task_id)
            if isinstance(task, str):
                return task
        if task.proc.poll() is None:
            task.note = task.note or "stopped on request"
        kill_group(task.proc)
        return self._status(task)

    def updates(self) -> list[str]:
        notes = []
        with self._lock:
            for task in self._tasks.values():
                ended = task.proc.poll() is not None and not task.ended_reported  # before the read: nothing is missed
                new, skipped = self._read_new(task, UPDATE_CHARS)
                if not (new or ended):
                    continue
                task.ended_reported |= ended
                cut = f"[{skipped} earlier bytes skipped; the full text is in {self._rel(task.log)}]\n" if skipped else ""
                notes.append(f"Background task {task.id} ({task.description or task.command[:80]}): "
                             f"{self._status(task)}\n{cut}{new}".rstrip())
        return notes

    def stop_all(self) -> None:
        with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
        for task in tasks:
            kill_group(task.proc)
