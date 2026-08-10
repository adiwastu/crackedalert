---
name: shipservice
description: Run the standard ship routine for a change to the Cracked Alert backend service — research, bump the static patch version, make edits, run a multi-pass verification loop, commit + push to main, then give the exact VPS deploy command. Use for any code change you want verified and shipped.
---

# shipservice

The standard workflow for shipping a change to the Cracked Alert live backend service, with verification baked in.

## When to use
Use this for any task that produces a code change to ship: a bug fix, feature, config tweak, or dependency change. If the task is "investigate only" (no code change), skip the version bump / commit / push / deploy parts.

## Versioning model
- **Runtime version** (`src/crackedalert/__init__.py` → `version()`): derives `v2.<git commit count>` from git. Auto-increments every commit/deploy — do not touch.
- **Static package version**: duplicated in `pyproject.toml` (`[project] version`) and `src/crackedalert/__init__.py` (`__version__`). Used as the pip/wheel install fallback. Bump the **patch** on every ship and keep both files **in sync**.

## The workflow

### 1. Bump the static patch version
Read the current version from `pyproject.toml`. Increment the patch (e.g. `2.0.0` → `2.0.1`). Apply the **same** value to:
- `pyproject.toml` → `version = "X.Y.Z"`
- `src/crackedalert/__init__.py` → `__version__ = "X.Y.Z"`

### 2. Research first
Read the relevant files before making any change. Ground every decision in the actual code. Report what you find.

### 3. Make the change
Apply the smallest, most targeted edit(s) that satisfy the task.

### 4. Multi-pass verification loop (MANDATORY — default 3 passes)
After implementing, run the **entire verification loop 3 complete times** (not once). Each pass is a fresh, independent review of the same checks. **Fix anything you find before moving to the next pass.** Do not skip or combine passes.

Each pass must include:
- **Static code audit:** Re-read every file you changed. Confirm the change is correct, complete, and consistent with codebase conventions (no hardcoded secrets, existing logging patterns, standard library + listed deps only).
- **Search sweep:** grep/file-search the relevant terms/strings to confirm no leftover references, dead code, or unintended side-effects. Confirm the version string in `pyproject.toml` and `src/crackedalert/__init__.py` match.
- **Tests:** Run `python -m unittest discover -s tests` and confirm 0 failures (the suite is unittest-based).
- **Compile check:** Run `python -m compileall src` and confirm 0 errors (package builds).
- **Logic trace (for bug fixes):** Walk through the affected flow and confirm it resolves correctly.

Run this loop **3 times**, documenting each pass. Only proceed after all 3 pass.

### 5. Commit + push
- Stage only relevant changed files (the task's changes + the version bump).
- Commit with a clear message (e.g. `fix: ...`, `chore: ...`, `feat: ...`). If the version bump is the only change, use `chore: bump version to X.Y.Z`.
- Push: `git push` (deploy pulls `origin main`).

### 6. Give the VPS deploy command
This is a backend-only service — **do not deploy locally**. End the report with the exact command the user runs on the VPS:
- The repo checkout on the VPS is at `~/crackedalert` (i.e. `/root/crackedalert`); `deploy_v2.sh` installs the app into `/opt/crackedalert`.
- SSH to the VPS, `cd ~/crackedalert`, and run `sudo ./deploy_v2.sh`
- `deploy_v2.sh` does: `git pull origin main`, installs into the venv, runs the connection smoke test (via `python -m crackedalert --smoke`), and restarts the `cracked-bot` systemd service.
- If a bare `git pull` + manual restart is preferred: `cd ~/crackedalert && git pull`, then `sudo systemctl restart cracked-bot` (after installing).

### 7. Report
Report: (1) old → new version bump, (2) research findings, (3) exact changes (files + rationale), (4) outcome of **each** verification pass, (5) test/compile results, (6) git commit hash + push confirmation, (7) the VPS deploy command, (8) residual observations.

## Notes
- Treat tests/compile as non-interactive: run them, wait for them to finish, don't bail early.
- Do not weaken auth/security globally; scope fixes to the affected flow.
- Never commit local secrets or `.env` files; they live on the VPS under `/etc/cracked_alert/` and are installed by `deploy_v2.sh`.