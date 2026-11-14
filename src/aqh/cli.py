"""`aqh`: the MAF Harness command line. H0 offers `--version` and `doctor`; the agent arrives in H1 to H6."""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys

from aqh import __version__

NEEDS = {"python": (3, 12), "node": (22, 18), "git": (2, 0)}  # node: the agentic-qe plugin's TypeScript runs natively from 22.18


def _version(program: str) -> tuple[int, ...] | None:
    """The installed version of a program, read from `<program> --version`, or None when it is missing or unreadable."""
    if program == "python":
        return sys.version_info[:3]
    path = shutil.which(program)
    if not path:
        return None
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out)
    return tuple(int(part) for part in found.groups() if part is not None) if found else None


def doctor() -> int:
    """Checks the tools the harness needs and says what is missing. Exit 0 only when every one is new enough."""
    ok = True
    for program, minimum in NEEDS.items():
        found = _version(program)
        want = ".".join(map(str, minimum))
        if found is None:
            print(f"FAIL {program}: not found (needs {want} or newer)")
            ok = False
        elif found[: len(minimum)] < minimum:
            print(f"FAIL {program}: {'.'.join(map(str, found))} is older than {want}")
            ok = False
        else:
            print(f"ok   {program}: {'.'.join(map(str, found))}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aqh", description="MAF Harness: quality engineering on a real repository.")
    parser.add_argument("--version", action="version", version=f"aqh {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("doctor", help="check the tools the harness needs")
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return doctor()
    parser.print_help()
    return 0
