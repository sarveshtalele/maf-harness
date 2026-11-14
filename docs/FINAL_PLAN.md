# MAF Harness: final plan (feature by feature)

Owner direction, 2026-11-14: build the harness in this repository only, reuse the code we have, do not repeat the
mistakes, list every feature, **code with Sonnet and review with Opus**. CLI first; fully separate from Agentic QE
Platform. The design behind it: `HARNESS_PLAN.md` (guides and sensors) and `HARNESS_ENGINEERING.md` (architecture, the
15 measured mistakes M1 to M15, patterns).

## 1. How every feature is built

```
Opus (this session's lead)   picks the next feature from section 4, writes its brief: goal, files, legacy to reuse,
                             mistakes to avoid, acceptance tests
Sonnet (implementer agent)   ports or writes the code and its tests, runs pytest, reports the real output
Opus (reviewer agent)        reviews the diff against the brief and the checklist in section 2; returns findings
Sonnet                       fixes the findings; tests again
Opus (lead)                  checks the tests passed, updates the tracker (section 6), commits after the owner's rules
```

- The implementer gets one feature at a time, never two that touch the same files.
- The reviewer never edits; it reports findings with file, line and reason.
- A feature is done only when its acceptance tests pass locally and in CI on Windows, macOS and Linux.

## 2. Review checklist (every feature, every review)

1. **No platform dependency.** Nothing imports `backend.*` or `legacy.*`; ported code is rewritten into `src/aqh/` (M1).
2. **One way per job.** No second tool, setting or code path for something that already exists (M2, M13).
3. **Small fixed prompt.** Any change to the tool list or instructions states its size effect; the budget is under
   8,000 tokens per call and about 15 tools per mode (M3).
4. **A definition of done.** Anything that loops has a stop condition, a budget and a test proving it stops (M4).
5. **Every control is a guide or a sensor** and names whether it is computational or inferential; sensor messages say
   what is wrong, where, and how to fix it.
6. **Stable prefix.** Volatile facts go after the history, never into the system instructions (M15).
7. **Honest failures.** No invented data, no `assert True`, no skips to get green; zero executed tests is INCONCLUSIVE.
8. **Safety.** Argument lists, timeouts, clean environment; paths through the path guard; no secrets in logs, prompts or
   this public repository.
9. **Cross-platform.** `pathlib`, `shutil.which`; a test runs on all three systems or says why it skips.
10. **Measured.** New behaviour is visible in the per-turn metrics (calls, repeats, tokens, cached tokens, seconds).

## 3. Code we reuse

| Source | What | How |
|---|---|---|
| `legacy/src/backend/maf/harness_kit/` (30 modules) | shells, sandbox, permissions, hooks, checkpoints, plugin contracts, skills, budgets, guards | ported module by module into the new design (section 4), each with its legacy tests adapted |
| `legacy/src/aqh/` | CLI, REPL, headless mode, settings, keychain | the CLI front end is reused; its server client is replaced, because the new harness runs in-process (no platform server) |
| `legacy/src/backend/maf/unique_calls.py` | unique tool-call ids, model I/O capture | into the model gateway (F02) |
| `legacy/tests/` (30 files) | behaviour that was proven | adapted to the new modules; kept as regression tests |
| Agentic QE Platform `src/backend/core/` at commit `94aa26a` | `plugins.py`, `plugin_hooks.py`, `settings_files.py`, `script_runtime.py`, `security.py` (redact) | the harness needs these but they stayed in the platform: ported here, trimmed to what the harness uses |
| `plugins/agentic-qe` (the plugin's own repository, pinned) | QE commands, skills, scripts, validators, hooks | used as the first plugin; pinned by commit, not copied by hand |

## 4. Features

| ID | Feature | Phase | Reuses | Must avoid | Guides and sensors | Acceptance |
|---|---|---|---|---|---|---|
| F01 | CLI shell: `aqh`, `aqh -p` headless, `--continue`, config and keychain for the model key | H6 (shell now, full later) | `legacy/src/aqh/*` | M1 | — | interactive and headless runs on 3 OS; key never printed |
| F02 | Model gateway: OpenAI-compatible client, unique call ids, retry with Retry-After, stall detection, circuit breaker, typed cost, I/O capture | H1 | `unique_calls.py`, platform `maf/llm.py` | M11, M12 | — | recorded 429 and stall replays resume; cost per turn reported |
| F03 | Session event log: append-only JSONL, tool call logged before it runs, resume, replay | H1 | `snapshots.py` ideas | M8, M11 | — | kill mid-tool, resume, no lost or doubled call |
| F04 | Turn state machine and approvals: Idle, CallingModel, AwaitingApproval, RunningTool, StepCheck, Done; parallel approval batches | H1 | approval lessons (M8 fix) | M4, M8, M13 | — | batches of 1, 3 and 5 approvals; deny and timeout paths |
| F05 | Request builder and context: stable prefix, pinned facts last, compaction, read cache | H1 | `tool_hygiene.py`, `active_command.py` (pinning), compaction setup | M3, M5, M15 | guide: pinned step facts | fixed prompt under 8,000 tokens; repeated read returns a pointer; cache hits measured |
| F06 | Tool registry and tool surface per mode: about 15 tools, deferred catalogue, aliases for guessed names, strict arguments | H2 | `catalog.py`, `unknown_tools.py`, `strict_args.py` | M2, M3 | sensor: unknown-name hint (computational) | offered tools per mode listed in a test; `read_file` alias runs `file_access_read` |
| F07 | Workspace file tools and path guard | H2 | `workspace.py`, MAF file access | M2 | sensor: path guard | writes outside the workspace refused |
| F08 | Shells: bash and PowerShell, every command starts at the workspace root, output spill, timeouts, background tasks | H2 | `shells.py`, `shell.py`, `shell_tools.py`, `bg_tasks.py` | M7 | sensor: exit code and spill notes | legacy shell tests pass; `cd` does not carry |
| F09 | Repository environment and OS sandbox: repo's own venv or node_modules, no harness PYTHONPATH, Seatbelt or bubblewrap | H2 | `os_sandbox.py`, env cleaning | M1 | — | repo tests run in the repo's environment; network blocked when sandboxed |
| F10 | Permissions, modes, settings files and hooks (allow, ask, deny; plan and execute; PreToolUse, PostToolUse, Stop) | H2 | `permissions.py`, `hook_middleware.py`, `prompt_hooks.py`, `stop_hooks.py`, platform `settings_files.py`, `plugin_hooks.py` | M13 | guides: rules; sensors: hooks | legacy permission and hook tests pass |
| F11 | Plugins and skills: plugin commands, skills of the active plugin only, skill scripts in any language | H3 | `skill_scopes.py`, `skill_runner.py`, `active_command.py`, platform `plugins.py`, `script_runtime.py` | M3, M9 | guides: skill text, references | only the active plugin's skills are listed; a TypeScript and a Python skill script run |
| F12 | Step contract and controls engine: done = files written and sensors pass; budget 180 s and 40 tool calls; sensors after each write, at step end, at a gate; fix messages | H3 | `plugin_contract.py`, `verify.py`, `write_first.py`, `budget.py` | M4, M9, M10 | sensors: contracts, quotes on disk, Gherkin parser, `check-gate.ts`, `qe_validate.py` | each sensor catches its seeded defect; a step ends when done |
| F13 | Checkpoints and `/rewind` | H2 | `checkpoints.py` | — | — | rewind restores files and transcript |
| F14 | Subagents with a lifecycle: brief, budget, collected result, no duplicate starts | H5 | `specialists.py`, `plugin_agents.py` | M6 | sensor: duplicate-start refusal | a started subagent's result is always collected |
| F15 | Harness templates: Python, JS/TS, Java, .NET, Go (toolchain, runner, linters, conventions) | H4 | — | M1 | guides and sensors per stack | a sample repository per template through Execute |
| F16 | Inferential sensors and mutation testing of generated tests; harness coverage report | H5 | plugin reviewer agents | M14 | sensors: reviewers, judge, mutation score | coverage report per stage from seeded defects |
| F17 | Observability: per-turn metrics, OpenTelemetry spans, evidence folder per run | H1 onward | run capture from the platform live runner | M12 | — | every turn writes its metrics line |
| F18 | Evals and replay: record a live session, replay with a stub model, discovery eval scorer | H1 onward | `test_discovery_eval_scoring.py`, platform `maf/evals/discovery.py` (commit `94aa26a^`) | M14 | — | replay of a recorded live session passes in CI |
| F19 | Live acceptance: the agentic-qe cycle to Gate 5 on two repositories | H7 | `test_qe_cycle_live.py` | all | all of the above | every stage 180 s or less, 40 tool calls or fewer, Gates 0 to 5 in the plugin ledger |

## 5. Order

H1: F02, F03, F04, F05, F17, F18 (core, testable offline). H2: F06, F07, F08, F09, F10, F13. H3: F11, F12. H4: F15.
H5: F14, F16. H6: F01 complete. H7: F19. One feature at a time through section 1's loop; the next starts only when the
previous one is done and committed.

## 6. Tracker

| ID | State | Evidence |
|---|---|---|
| H0 | done | `b44f7b8`: CLI skeleton, CI on 3 OS, `legacy/` |
| F01 to F19 | not started | |
