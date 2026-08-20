---
name: deploy
description: Deploy the Cracked Alert backend service to the VPS — verify tests pass, commit + push to main, then give the exact SSH deploy command. Use for /deploy (deploying an already-shipped change).
---

# deploy

The standard workflow for deploying a change to the Cracked Alert live backend service.

## When to use
Use for deploying — no new code change. If the task still needs code changes, use the `shipservice` skill instead (research, version bump, changes, verification, commit + push, deploy).

## The workflow

### 1. Verify (if not already done)
Run `python -m unittest discover -s tests -q` and confirm 0 failures. If not already committed, commit the feature changes first.

### 2. Commit + push
- Stage only relevant changed files.
- Commit with a clear message (e.g. `fix: ...`, `chore: ...`, `feat: ...`).
- Push: `git push` (the VPS pulls `origin main`).

### 3. Flag backend changes
This repo is a backend-only service — there is **no frontend deploy**. Every change is a backend change; the user deploys on the VPS manually. State that explicitly.

### 4. Give the VPS deploy command
The exact command the user runs on the VPS:
```
ssh root@104.64.205.15
cd ~/crackedalert
sudo ./deploy_v2.sh
```
- `deploy_v2.sh` does: `git pull origin main`, installs into the venv, runs the connection smoke test (`python -m crackedalert --smoke`), and restarts the `cracked-bot` systemd service.
- If a bare pull + manual restart is preferred: `cd ~/crackedalert && git pull`, then `sudo systemctl restart cracked-bot` (after venv install).
- DB migrations (e.g. new columns) run automatically on service start — no manual migration needed.
- Check: `systemctl status cracked-bot` and `journalctl -u cracked-bot -f`.

### 5. Report
Report: (1) git commit hash + push confirmation, (2) test result, (3) clear statement that this is a backend-only change, (4) the exact SSH deploy command.

## Notes
- Treat tests as non-interactive: run them, wait for them to finish, don't bail early.
- Never commit local secrets or `.env` files; they live on the VPS under `/etc/cracked_alert/` and are installed by `deploy_v2.sh`.