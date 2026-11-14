"""Two loop-hygiene guards from the strongest harnesses (FINAL_PLAN 5.3), as one MAF function middleware.

- Repeat reminder (DeepSeek Harness `repeat-tool-reminder`): the same tool with the same arguments for the 3rd, 5th
  and 8th time in a turn gets a note asking the model to use the earlier result or change approach. Advisory only.
- Oversized results: a result over `LIMIT` characters keeps its head and tail and says how much was cut and how to
  read a range (Claude Code clears old tool output; DeepSeek Harness trims over 8,192 characters).
  ponytail: no spill files; a range read gets the rest. Spill to a file if ranges prove not enough.
- Read cache (docs/HARNESS_MEMORY_AND_TOOLS.md 3.2, tier 1): a repeated identical read in the same thread, with nothing
  written since, returns a one-line pointer to the earlier result instead of the full text again (live QE cycle
  2026-11-13: one skill loaded 26 times, pyproject.toml read 20 times in one stage). A write or a shell command makes file
  results stale; a compaction that removes history forgets the thread's cache, so a pointer never points at nothing.
"""
from __future__ import annotations

import json
from collections import Counter

from agent_framework import FunctionInvocationContext, function_middleware
from pydantic import BaseModel

from backend.maf.runtime.factory import _text

REMIND_AT = (3, 5, 8)
FILE_READS = frozenset({"file_access_read", "file_access_read_lines", "file_access_ls", "file_access_grep"})
STATIC_READS = frozenset({"load_skill", "read_skill_resource", "describe_contract"})  # content that does not change in a turn
CHANGES_FILES = frozenset({"file_access_write", "file_access_replace", "file_access_replace_lines", "file_access_delete",
                           "bash", "powershell", "run_shell", "run_skill_script"})
POINTER = ("[harness] Not repeated: this is the same call as an earlier one in this conversation and nothing was written since, "
           "so its result above is still current. Use it, or read a different file or range.")
_cached: dict[str, set] = {}  # thread -> keys whose result is in the conversation; ponytail: per process, a restart only re-reads
_epoch: dict[str, int] = {}  # thread -> count of calls that may have changed files


def forget(thread_id: str) -> None:
    """History was compacted: earlier results may be gone, so every read runs again."""
    _cached.pop(thread_id, None)
LIMIT, HEAD, TAIL = 12000, 8000, 2000


def tool_hygiene(thread_id: str = ""):
    """The middleware for one turn (its repeat counts start empty; the read cache is the thread's)."""
    seen: Counter = Counter()

    @function_middleware
    async def hygiene(context: FunctionInvocationContext, call_next) -> None:
        args = context.arguments.model_dump() if isinstance(context.arguments, BaseModel) else dict(context.arguments or {})
        key = (context.function.name, json.dumps(args, sort_keys=True, default=str))
        seen[key] += 1
        name = context.function.name
        if name in CHANGES_FILES:
            _epoch[thread_id] = _epoch.get(thread_id, 0) + 1
        cache_key = (key, 0 if name in STATIC_READS else _epoch.get(thread_id, 0)) if name in FILE_READS | STATIC_READS and thread_id else None
        if cache_key and cache_key in _cached.get(thread_id, set()):
            context.result = POINTER
            return
        await call_next()
        text = _text(context.result)
        if cache_key and not text.lstrip().startswith(("Error", "Refused")):
            _cached.setdefault(thread_id, set()).add(cache_key)
        if len(text) > LIMIT:
            text = (f"{text[:HEAD]}\n\n[harness] {len(text) - HEAD - TAIL} characters cut from the middle of this {len(text)}-character result. "
                    f"Read a range (file_access_read_lines) for the rest.\n\n{text[-TAIL:]}")
            context.result = text
        if seen[key] in REMIND_AT:
            context.result = (f"{text}\n\n[harness] This is call {seen[key]} of {context.function.name} with these same arguments in this turn. "
                              "Use the earlier result, change the approach, or finish.")

    return hygiene
