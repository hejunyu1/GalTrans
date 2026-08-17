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

