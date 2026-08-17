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

## AI engineering quality guardrails

- Work on one roadmap milestone at a time and keep changes small enough to review and reverse.
- Inspect the current implementation and tests before changing behavior; do not invent project
  state from conversation memory.
- Add or update tests for every behavior change and every fixed bug. Never weaken, skip, or delete
  a failing test merely to make a change pass.
- Prefer the simplest design that meets the current milestone. Do not add speculative frameworks,
  abstractions, dependencies, or compatibility layers without a concrete present need.
- Keep deterministic engine handling separate from model-driven language work. A model proposal
  must cross a typed, schema-validated boundary before deterministic code can use it.
- After two substantially similar failed attempts, stop repeating the same approach. Re-read the
  evidence, state the unresolved cause, and either try a meaningfully different method or ask the
  user for the exact missing input.
- Keep documentation explicit about what is implemented, proposed, unsupported, and unverified.
  Do not describe planned behavior as complete.
- Before committing a version, review the full diff for accidental scope growth, duplicated logic,
  secrets, generated files, unauthorized content, and changes unrelated to the milestone.

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

- At the end of each completed and verified code version or substantive product milestone, inspect
  the diff and Git status, stage only the intended files, create a descriptive Git commit, and push
  it to the existing `origin/main` GitHub branch without asking the user to run the commands.
- Routine rule, instruction, and documentation-only updates do not require a standalone GitHub
  push. They may remain local and be included with the next verified code version unless delaying
  them would create a concrete safety or collaboration risk; explain that risk before publishing
  them separately.
- A version is not ready to commit or push until its relevant tests and checks pass, durable
  documentation is synchronized, known limitations are reported, and no secrets, local
  environments, SDKs, generated outputs, or unauthorized game content are staged.
- Never force-push, rewrite published history, delete branches or tags, or bypass branch
  protection. Do not create version tags or GitHub Releases unless the user explicitly requests
  them.
- If committing or pushing fails, report the exact error and stop. Do not weaken safety checks or
  alter authentication and repository settings as a workaround without explicit user approval.
