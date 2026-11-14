"""MAF's OpenAI chat client with tool call ids that are unique within a conversation turn.

Found on the live QE cycle with gpt-5 (2026-11-13): this endpoint names tool calls `functions.bash:3` and reuses an id in
a later round of the same turn. MAF's AG-UI approval store keys an approval by that id, so the second call replaced the
first one's record, the resume failed with "Collected approval authority is no longer pending", a card stayed open, and
every later message was refused (APPROVAL_RESUME_REQUIRED). Each id the model sends gets a suffix unique to its response:
the same id across the chunks of one streamed response keeps one suffix, so arguments still merge.
"""
from __future__ import annotations

import itertools
import os
from pathlib import Path

from agent_framework.openai import OpenAIChatCompletionClient


def _capture(item) -> None:
    """Live-run diagnostics: with AQE_CAPTURE_MODEL_IO set to a folder, every raw model response and stream chunk is kept
    there as JSON lines (redacted), so an empty or wrong turn can be traced to exactly what the model sent back."""
    folder = os.environ.get("AQE_CAPTURE_MODEL_IO")
    if not folder:
        return
    from backend.core.security import redact

    Path(folder).mkdir(parents=True, exist_ok=True)
    with (Path(folder) / "model-io.jsonl").open("a", encoding="utf-8") as out:
        out.write(redact(item.model_dump_json(exclude_none=True)) + "\n")


class _Request:
    def __init__(self, **summary):
        self.summary = summary

    def model_dump_json(self, **_):
        import json

        return json.dumps({"request": self.summary})


class UniqueCallIdClient(OpenAIChatCompletionClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._response_key = ""
        self._ids: dict[tuple[str, str], str] = {}
        self._counter = itertools.count(1)

    def _unique(self, contents: list) -> list:
        for item in contents:
            original = getattr(item, "call_id", None)
            if getattr(item, "type", "") == "function_call" and original:
                key = (self._response_key, original)
                if key not in self._ids:
                    self._ids[key] = f"{original}~{next(self._counter)}"
                item.call_id = self._ids[key]
        return contents

    def _prepare_options(self, messages, options):
        prepared = super()._prepare_options(messages, options)
        if os.environ.get("AQE_CAPTURE_MODEL_IO"):  # the size of what the model is sent, to see history kept or lost
            sent = prepared.get("messages") or []
            roles = [m.get("role", "") for m in sent if isinstance(m, dict)]
            _capture(_Request(messages=len(sent), roles={r: roles.count(r) for r in set(roles)}, chars=len(str(sent))))
        return prepared

    def _parse_response_from_openai(self, response, options):
        _capture(response)
        self._response_key = getattr(response, "id", "") or f"r{next(self._counter)}"
        return super()._parse_response_from_openai(response, options)

    def _parse_response_update_from_openai(self, chunk):
        self._response_key = getattr(chunk, "id", "") or self._response_key
        _capture(chunk)
        return super()._parse_response_update_from_openai(chunk)

    def _parse_tool_calls_from_openai(self, choice):
        return self._unique(super()._parse_tool_calls_from_openai(choice))
