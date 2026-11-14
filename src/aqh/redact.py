"""Mask anything secret-shaped before it reaches the model, a log or a file.

Ported, trimmed to redaction only (no encryption, no scanning), from the agentic-harness `core/security.py`
(`SECRET_PATTERNS`, `redact_value`, `register_secret`, `redact`, `MIN_MASKED`, `_LOGIN_FIELD`), commit 94aa26a.
Deviations from the source, each fixing a false positive: the `sk-`, `gh*_` and `AKIA` patterns need a left boundary
(so `task-runner-configuration.ts` is not a key); `pwd` is not a login field (`PWD=/Users/me/repo` is a path);
`password:` fires only when a quote follows (`password: str = None` is a type annotation), `password=value` always.
ponytail: process-wide set of registered secrets and pattern matching only; a secret split across two chunks of
streamed output is not caught. Upgrade: redact on the joined text, or add an entropy check.
"""
from __future__ import annotations

import re

_B = r"(?<![A-Za-z0-9_-])"  # not in the middle of a longer word such as task-runner or disk-usage
SECRET_PATTERNS = [
    (re.compile(_B + r"sk-ant-[A-Za-z0-9_\-]{20,}"), "Anthropic API key"),
    (re.compile(_B + r"sk-[A-Za-z0-9_\-]{20,}"), "OpenAI-style API key"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "GitHub fine-grained token"),
    (re.compile(_B + r"gh[pousr]_[A-Za-z0-9]{30,}"), "GitHub token"),
    (re.compile(r"glpat-[A-Za-z0-9_\-]{20}"), "GitLab token"),
    (re.compile(_B + r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "Private key"),
    (re.compile(r"(?i)\b(?:api[_-]?key|secret[_-]?key|auth[_-]?token|access[_-]?token|password)\b\s*[:=]\s*"
                r"['\"][^'\"\s]{12,}['\"]"), "Hardcoded secret"),
]
_LOGIN_FIELD = re.compile(
    r"""(?i)(["']?\b(?:password|passwd)\b["']?\s*(?:=|:(?=\s*["'])))(\s*["']?)[^\s"',}]+""")
_known_secrets: set[str] = set()

# A shorter secret would be masked as a substring of every text ("admin" would turn every "admin" into ***).
MIN_MASKED = 12


def redact_value(value: str) -> str:
    return value[:4] + "…" + value[-2:] if len(value) > 10 else "***"


def register_secret(value: str) -> None:
    """A value that must never appear in any text the model reads. Values shorter than MIN_MASKED are ignored."""
    if len(value) >= MIN_MASKED:
        _known_secrets.add(value)


def redact(text: str) -> str:
    for value in _known_secrets:
        text = text.replace(value, "***")
    for pattern, _ in SECRET_PATTERNS:
        text = pattern.sub(lambda m: redact_value(m.group(0)), text)
    return _LOGIN_FIELD.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
