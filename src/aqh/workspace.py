"""The workspace: one repository root plus a scratch folder, and the only path guard (`Workspace.resolve`).

Every file and shell tool calls `resolve`; nothing else decides whether a path is allowed.
TOCTOU: callers must open the returned path itself; a symlink swapped in between is a ceiling (upgrade: O_NOFOLLOW/openat).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

# Parts of a repository the agent may read but never write. Compared case-insensitively on every OS (stricter than
# needed on Linux, harmless).
PROTECTED = frozenset({".git", ".venv", "node_modules"})


class PathRefused(PermissionError):
    """A path the guard refused. The message is written for the model: what, where, how to fix it."""


def windows_problem(path: str, drives: set[str]) -> str | None:
    """Why a Windows-style path is unsafe (UNC or device path, other drive, drive-relative), else None. Pure, so
    it is tested on every OS; `resolve` applies it only on Windows, where these forms mean something."""
    p = PureWindowsPath(path)
    if p.drive.startswith("\\\\"):
        return "it is a network (UNC) or device path"
    if p.drive and p.drive.casefold() not in drives:
        return f"it is on drive {p.drive}, not the workspace drive"
    if p.drive and not p.root:
        return "it is relative to a drive's current folder"
    return None


@dataclass(frozen=True)
class Workspace:
    root: Path
    scratch: Path

    @classmethod
    def open(cls, root: str | Path, scratch: str | Path | None = None) -> Workspace:
        root = Path(root).resolve()
        if not root.is_dir():
            raise ValueError(f"Workspace root {root} is not an existing directory. Pass the repository folder.")
        scratch = (root / scratch).resolve() if scratch else root / ".aqh" / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        return cls(root, scratch)

    def resolve(self, path: str | Path, *, for_write: bool = False) -> Path:
        """Absolute, symlink-free path inside root or scratch. Relative paths start at root, never at the cwd."""
        fix = f"Use a path inside {self.root} (or the scratch folder {self.scratch})."
        if sys.platform == "win32":
            drives = {PureWindowsPath(str(b)).drive.casefold() for b in (self.root, self.scratch)}
            if why := windows_problem(str(path), drives):
                raise PathRefused(f"Refused {path}: {why}. {fix}")
        try:
            resolved = (self.root / path).resolve()  # an absolute `path` replaces root; `..` and symlinks are followed
        except (ValueError, OSError) as e:
            raise PathRefused(f"Refused {path!r}: it is not a valid path ({e}). {fix}") from e
        base = next((b for b in (self.root, self.scratch) if resolved.is_relative_to(b)), None)
        if base is None:
            raise PathRefused(f"Refused {path}: it resolves to {resolved}, outside the workspace. {fix}")
        if for_write:
            bad = next((p for p in resolved.relative_to(base).parts if p.casefold() in PROTECTED), None)
            if bad:
                raise PathRefused(f"Refused write to {resolved}: '{bad}' is protected "
                                  f"({', '.join(sorted(PROTECTED))}). You may read it; write somewhere else in {self.root}.")
        return resolved
