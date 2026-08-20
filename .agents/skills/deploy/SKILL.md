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
This repo is a backend service plus a static frontend UI (`frontend/ui.html`). `deploy_v2.sh` handles both: it installs/updates the backend service **and** copies `frontend/` to `/var/www/crackedalert-ui` and appends/reloads the Caddy site block (`deploy/caddy-ui.caddyfile`) that owns `:80` on the box. If the change touches the frontend, mention the live URL to verify.

### 4. Give the VPS deploy command
The exact command the user runs on the VPS:
```
ssh root@104.64.205.15
cd ~/crackedalert
sudo ./deploy_v2.sh
```
- `deploy_v2.sh` does: `git pull origin main`, installs into the venv, runs the connection smoke test (`python -m crackedalert --smoke`), restarts the `cracked-bot` systemd service, and serves the static UI via Caddy at `http://hotland3x3.my.id/ui.html`.
- If a bare pull + manual restart is preferred: `cd ~/crackedalert && git pull`, then `sudo systemctl restart cracked-bot` (after venv install). For frontend-only changes, `sudo ./deploy_v2.sh` still covers the UI copy + Caddy reload after `git pull`.
- DB migrations (e.g. new columns) run automatically on service start — no manual migration needed.
- Check: `systemctl status cracked-bot` and `journalctl -u cracked-bot -f`; UI check: visit `http://hotland3x3.my.id/ui.html` and confirm `ss -ltnp | grep ':80'` still shows only `caddy`.

### 5. Report
Report: (1) git commit hash + push confirmation, (2) test result, (3) whether the change is backend, frontend, or both, (4) the exact SSH deploy command.

## Notes
- Treat tests as non-interactive: run them, wait for them to finish, don't bail early.
- Never commit local secrets or `.env` files; they live on the VPS under `/etc/cracked_alert/` and are installed by `deploy_v2.sh`.