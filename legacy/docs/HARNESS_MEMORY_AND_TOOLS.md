# Harness architecture, level 2: memory, tool discovery and step control

Owner request, 2026-11-13: each QE stage must finish in two to three minutes with accurate output; architect the harness
for memory and tool discovery before patching further. This document starts from measurements, not guesses.

## 1. What was measured (2026-11-13, deepseek-v4-flash-global, `personal-finance-mcp`)

| Measure | Value | Source |
|---|---|---|
| Discovery stage, before fixes | 1,174 s, 356 tool calls, 244 model calls | run `…-2140` session log |
| Repeated calls in that stage | 80%: one skill loaded 26 times, `pyproject.toml` read 20 times, same analyst started 7 times and never collected | same |
| Files written | at 82 s; the remaining 1,090 s added nothing the gate needed | same |
| Fixed cost of every model call | 45 tool schemas (30,360 characters) + 32,426 characters of instructions, about 15,000 tokens before any history | offline probe of a harness turn |
| Prompt tokens per model call | flat 16,500 to 20,000 | captured usage, run `…-2231` |
| Prompt cache hits at the endpoint | 69% of 3.5 M prompt tokens | captured usage, run `…-2140` |
| History across approval cards | kept: request grew from 3 to 174 messages over 50 resumes | live probe with request capture |
| Shell directory | `cd tests` carried to every later command; the model concluded the repository had no `pyproject.toml` and spent 5 minutes on venvs | live probe |
| Guessed tool names | `read_file`, `grep`, `list_files` called although removed in workspace mode | live probe |

What this rules out: memory is not lost between rounds or approval cards (both measured), and the endpoint already
caches prompt prefixes. What it shows: the model makes far too many calls, each one carries a heavy fixed prompt, and it
does not know when a step is finished.

## 2. Fixed already (this session)

| Fix | Effect | Test |
|---|---|---|
| A step is done when its files are on disk and its check passes; open todos no longer keep the turn going, and inside a round one extra tool call gets a reminder, the next ends the loop | discovery 1,174 s to 252 s | `tests/test_harness_step_done.py` |
| Gates approved in the plugin's ledger are shown to the model; files waiting for a gate are owed once it is approved | intake files written after Gate 0 | `tests/test_harness_active_command.py` |
| A chat request for `approve create` goes on to the write (the card is the yes) | discovery writes its files | `tests/test_harness_hooks_settings.py` |
| Parallel approval batches of 3+ resume (MAF AG-UI 1.8 defect repaired in our store) | no `Collected approval authority` failures | `tests/test_harness_parallel_approvals.py` |
| Every shell command starts at the workspace root | no directory drift | `tests/test_harness_shells.py` |

## 3. Design

### 3.1 Tool surface per phase (tool discovery)

One small offered set per kind of turn; everything else stays in the deferred catalogue (`find_tools`, `load_tools`,
already built) so the model can still reach it by purpose.

| Turn | Offered tools (about 15) | Deferred |
|---|---|---|
| Plugin step (`/agentic-qe:*` active) | `file_access_ls`, `file_access_read`, `file_access_read_lines`, `file_access_grep`, `file_access_write`, `file_access_replace`, `bash`, `check_gate`, `describe_contract`, `load_skill`, `read_skill_resource`, `todos_add`, `todos_complete`, `find_tools`, `load_tools` | memory files, background agents, documents, findings, mode, sandbox status, scan, skill scripts, task output |
| Plain harness turn | the same plus `run_skill_script`, `task_output`, `task_stop`, `mode_set` | the rest |

Tool descriptions are cut to what the model needs to choose (at most about 300 characters each). Target: fixed prompt
under 8,000 tokens.

**Aliases instead of errors.** A call to a name models use from training (`read_file`, `Read`, `cat`, `grep`, `Grep`,
`list_files`, `Glob`, `ls`) runs the matching tool with its arguments mapped (`path` to `file_name`, `pattern` stays),
and the result says which tool ran. Today each guess costs a failed round and a hint.

### 3.2 Memory in four tiers

| Tier | Holds | Lives | Mechanism |
|---|---|---|---|
| 1. Working set | tool results of this turn | the request's message list (MAF function loop) | as now; plus a **read cache**: a repeated read of an unchanged file, a repeated `load_skill` of the same skill, or a repeated listing of an unchanged folder returns a one-line pointer ("unchanged; its content is above, call N") instead of the full text. Keyed by thread, tool, arguments and the file's size and modification time; cleared when compaction removes the earlier result |
| 2. Thread history | user turns, answers, tool calls and results across requests | MAF AG-UI thread snapshot (SQLite) and the session | as now (measured: kept across 50 resumes); at a **phase change** (a new plugin command) the previous phase's tool results are collapsed to one line each, since its files on disk now carry that knowledge |
| 3. Step facts | files read so far this step, files written, checks run, gates approved | pinned command context (`ActiveCommandContext`) | extend the pinned block with "already read: …" (paths only), so the model does not re-read to remember |
| 4. Project memory | repository facts, plugin outputs (`docs/qe/<app>/…`), ledger | disk and database | as now: the next phase reads the previous phase's files, not the chat |

**Prompt (KV) cache.** The endpoint caches identical prompt prefixes (69% measured). Anything that changes between
calls placed early in the prompt breaks the cache for everything after it. The volatile parts of the pinned block (file
state, gates, check status, already-read list) move from the instructions to a short context message placed after the
history, so instructions, tools and history stay one stable prefix. Target: 80% or more cached.

### 3.3 Step control

- **Done** = the command's files are on disk and its own check passes (built).
- **Budget per step**: 40 tool calls or 180 seconds. At the budget the harness asks for the files with what has been read
  so far, then ends the turn; the evidence records it. The gate check then lists real gaps for the fix turn.
- **Background agents** are deferred in plugin steps. When a step starts one, the harness collects its result into the
  conversation when it finishes, and refuses a second start with the same description.

### 3.4 Measured on every run

Per turn, written to the ledger and the live evidence: seconds, model calls, tool calls, repeated calls, prompt tokens,
cached tokens. The live runner prints a table per stage, and a stage over budget is flagged. The discovery eval
(`evals/harness/discovery.json`) stays the regression gate for tool choice.

## 4. Acceptance

| Measure | Now | Target |
|---|---|---|
| Discovery stage | 252 s, 97 tool calls | 180 s or less, 40 tool calls or fewer |
| Repeated tool calls per stage | 20% to 80% | 10% or less |
| Fixed prompt per model call | about 15,000 tokens | under 8,000 tokens |
| Prompt cache hits | 69% | 80% or more |
| QE cycle | stopped before Gate 1 | Gates 0 to 5 approved by the plugin's hook, with evidence |

## 5. Order of work

1. Read cache and aliases (3.1, 3.2 tier 1): small middlewares, unit tests with the scripted model.
2. Tool surface per phase and shorter descriptions (3.1): measured with the offline probe (tools, characters).
3. Volatile context after the history and the "already read" list (3.2 tiers 3 and cache).
4. Step budget and background agent handling (3.3).
5. Per-turn metrics in the ledger and the live evidence (3.4).
6. Live QE cycle to Gate 5 on `personal-finance-mcp`, then `mcp-server-market-v2`, each stage measured against section 4.
