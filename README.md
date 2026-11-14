# aqh

A quality-engineering harness on Microsoft Agent Framework (MAF): an agent that works on a real repository, steered by
**guides** (what good output looks like, given before it acts) and checked by **sensors** (deterministic checks first,
model judgement only where needed), with a named person at every gate.

Separate from Agentic QE Platform. Clients: the `aqh` command line and a Tauri desktop app; no web client for now.

| Status | |
|---|---|
| Phase | H0: repository, CI, legacy reference copy |
| Plan | [`docs/FINAL_PLAN.md`](docs/FINAL_PLAN.md) (every feature, order, tracker); [`docs/HARNESS_PLAN.md`](docs/HARNESS_PLAN.md) (guides and sensors) |
| Design and lessons | [`docs/HARNESS_ENGINEERING.md`](docs/HARNESS_ENGINEERING.md) (15 measured mistakes, patterns, state machine) |
| Origin | [`docs/HARNESS_SEPARATION_PLAN.md`](docs/HARNESS_SEPARATION_PLAN.md), [`legacy/PROVENANCE.md`](legacy/PROVENANCE.md) |

## Requirements

Python 3.12 or newer, Node.js 22.18 or newer (the agentic-qe plugin's TypeScript), Git.

## Try it

```bash
python3 -m venv .venv && .venv/bin/pip install -e . pytest
```

```bash
.venv/bin/aqh doctor
```

```bash
.venv/bin/pytest
```

On Windows use `py -m venv .venv` and `.venv\Scripts\aqh doctor`.

## Layout

| Path | Holds |
|---|---|
| `src/aqh` | The harness (H0: the CLI shell and `doctor`) |
| `tests` | Tests; CI runs them on Windows, macOS and Linux |
| `docs` | Plan, engineering design, separation plan |
| `legacy` | Read-only reference: the embedded harness as it was in Agentic QE Platform. Never imported; pieces are ported with their tests |
