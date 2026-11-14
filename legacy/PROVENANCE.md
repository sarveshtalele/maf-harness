# Provenance of `legacy/`

Copied on 2026-11-14 from the Agentic QE Platform repository (`sarveshtalele/agentic-harness`), branch
`feature/harness-rebuild`, commit `dfe25c333e1f803c526a358160fff1f41002b326`: the embedded MAF harness and everything
only it used (100 files): `src/backend/maf/harness.py`, `src/backend/maf/harness_kit/`, the harness agent policy, the
unique-call-id client, the `aqh` CLI, the Electron desktop app, the harness prompt and evals, its tests, and its
documents.

It is a **reference, not a dependency**. It imports `backend.*` modules of the platform that are not here, so it does
not run as is. Pieces are ported into the new design one at a time, each with its tests (`docs/HARNESS_PLAN.md`
phases H1 to H6). The full history stays in the platform repository.
