# GalTrans development instructions

## Product goal

Build a safe, reviewable localization workflow for visual novels. Deterministic code handles
game files and validation; language models may propose structured translations but never edit
game files directly.

## Current scope

- Windows-native development with Python 3.13 in `.venv`.
- Standard-library-only foundation until an external dependency has a concrete need.
- Ren'Py is the first engine adapter.
- Current adapters read source scripts only. They must not unpack protected games or modify input.

## Commands

```powershell
.\scripts\galtrans.ps1 doctor
.\scripts\galtrans.ps1 scan .\samples\renpy_demo
.\scripts\test.ps1
```

Before handing off a change, run the tests, `compileall`, and `git diff --check`.

## Safety invariants

1. Treat imported game content as read-only.
2. Never overwrite an output file unless the caller explicitly requests replacement.
3. Preserve raw bytes, encoding, protected tokens, and source hashes needed for verification.
4. Use self-authored or authorized fixtures only.
5. Prefer a warning or unsupported result over guessing about ambiguous script syntax.
6. Keep engine-specific parsing behind adapters and expose a shared intermediate representation.
7. Model responses must be schema-validated before they can enter an export pipeline.

## Documentation

- `docs/vision.md`: durable product direction and non-goals.
- `docs/architecture.md`: current and proposed system design.
- `docs/roadmap.md`: current milestones and exit criteria.
- `docs/decisions/`: durable architecture decisions.

Update documentation when a change invalidates it. Do not present proposed components as already
implemented.

## Conversation and task continuity

- Tell the user directly when it is safer to end the current Codex task and continue in a new
  conversation window. Do not silently continue when accumulated context is likely to reduce
  accuracy.
- Recommend a new window at a natural milestone boundary, when the next goal moves to a different
  subsystem, or when repeated failed attempts, superseded plans, or contradictory context are
  creating noise.
- Do not switch in the middle of an unverified change, a failing test run, or an unresolved
  investigation. First restore or reach a clear checkpoint and report anything unfinished.
- Before recommending a switch, verify the relevant tests and checks, synchronize the roadmap and
  architecture documents, report known limitations, inspect Git status, and provide a concise
  handoff summary. Tell the user directly if a commit or push is needed.
- Give the user a ready-to-copy opening prompt for the new conversation that names the next
  milestone and asks the new task to read `AGENTS.md`, the durable project documents, Git status,
  recent commits, and current tests before editing code.

## Version control and GitHub publishing

- At the end of each completed and verified version or coherent milestone, inspect the diff and
  Git status, stage only the intended files, create a descriptive Git commit, and push it to the
  existing `origin/main` GitHub branch without asking the user to run the commands.
- A version is not ready to commit or push until its relevant tests and checks pass, durable
  documentation is synchronized, known limitations are reported, and no secrets, local
  environments, SDKs, generated outputs, or unauthorized game content are staged.
- Never force-push, rewrite published history, delete branches or tags, or bypass branch
  protection. Do not create version tags or GitHub Releases unless the user explicitly requests
  them.
- If committing or pushing fails, report the exact error and stop. Do not weaken safety checks or
  alter authentication and repository settings as a workaround without explicit user approval.
