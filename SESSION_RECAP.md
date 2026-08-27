# Session Recap — Cracked Alert Trading Bot Fixes

**Date:** 2026-08-22
**Branch:** `main`
**Shipped commit:** `3a70fa2`
**Repo:** https://github.com/adiwastu/crackedalert.git

---

## 1. What this session was about

This session fixed **three bugs** in the **Cracked Alert** /cTracker trading bot —
a Python 3 Telegram trading bot (python-telegram-bot) that places trades on the
cTrader Open API and manages them with a price-alert engine.

The user reported:

| # | Bug | Symptom |
|---|-----|---------|
| 1 | **CC guard ("Smart SL with CC alert") can't find the current position** | After a trade, the guard either never gets set, or for pending orders the whole auto-alert chain dies with "position not found" / "could not be set (position confirm failed)". |
| 2 | **Auto TP/SL alerts are never placed** | After a market order fills, no TP/SL alerts appear; after a pending order fills, the chain dies before TP/SL alerts are created. |
| 3 | **Missing "better calculation" / lot multiplier** | The user's idea: put the stop *between* entry and the far SL so risk-per-lot is lower, and use the freed risk budget to run a **multiple** of the lot size. The backend math already existed (`x<mult>`), but the frontend had no control for it. |

The session started in PLAN mode with the **llm-verifier** skill to root-cause all
three bugs, then switched to ACT mode to implement, test, and ship the fixes.

---

## 2. Root causes

### Bug 1 — `confirm_position` never linked `orderId → positionId`

- `confirm_position()` matched positions ONLY if `position["positionId"] == order_id`
  (an int-vs-`orderId` comparison that can never be true for pending fills) or by a
  hardcoded ~0.05 price tolerance.
- **No `PT_EXECUTION_EVENT` subscription was ever registered**, so no
  `orderId → positionId` fill map existed.
- Pending orders fill asynchronously; `on_entry_hit` calls `confirm_position(order_id)`
  and finds nothing → guard fails, and the TP/SL chain dies.
- There was also **no retry loop**, so a just-filled position missing from the first
  reconcile snapshot was a permanent failure.

### Bug 2 — market entry alerts fired on MID, but entry was ask/bid

- The price-alert engine fires on **MID** price.
- For a market order, `_create_entry_alert` used `plan.entry_ref`, which is
  **ask** (BUY) / **bid** (SELL). MID never reaches ask (ask = mid + spread), so the
  entry alert **never fired** → `on_entry_hit` never ran → no TP/SL alerts.
- For a just-filled market order the entry is *already* hit — creating a
  target-based entry alert is fundamentally wrong.

### Bug 3 — the math existed, the UI didn't

- `risk.py` already implemented `mult`: when `mult > 1`, the effective SL distance
  = original dist / mult (SL pulled toward entry), `lots × mult`, same dollar risk,
  same TP based on original dist × RR.
- The parse layer already accepted `x<mult>` (e.g. `/m 2440 y 2 0.5 10k x2 M15`).
- But `command_builder.html` / `frontend/ui.html` had **no multiplier control**, so
  the feature was unreachable from the UI.

---

## 3. The fixes

### Fix 1a — harden `confirm_position` (`src/crackedalert/ctrader/trading.py`)

- Added an `orderId → positionId` fill map on `TradingService`
  (`note_fill()` / `filled_position_id()`).
- `confirm_position()` now:
  1. Resolves the incoming `trade_id` (orderId) to a `positionId` via the fill map.
  2. Compares positionIds as **ints** (an orderId string can never false-match).
  3. Derives entry tolerance from the symbol's **digits** (~1.5 pips, floored at a cent)
     instead of a hardcoded `0.05`.
  4. **Retries 3×** with a short delay before giving up (just-filled orders appear late).

### Fix 1b — wire the execution-event stream (`src/crackedalert/main.py`)

- Registered a `PT_EXECUTION_EVENT` handler (`on_execution`) on every client that
  maps `orderId → positionId` into `trader.note_fill()` as fills arrive live.
- `execute()` also records the fill map when a fill response carries both ids.

### Fix 2 — market orders create TP/SL immediately (`src/crackedalert/bot/handlers.py`)

- **Market path:** skip the engine entry alert entirely. Immediately create the TP
  and SL alerts (broadcast to all subscribers) via a new `_create_tp_sl_alerts`
  helper that mirrors what `on_entry_hit` does, and attach the cc guard through the
  fast path using `result.position/position_id` (no confirm needed).
- **Pending path:** keep the entry-alert → `on_entry_hit` flow. The entry alert fires
  correctly on MID (placement price), and `on_entry_hit` now resolves the filled
  position via the bug-1 fix, then creates TP/SL + the cc guard on fill.
- The "use /ccalert manually" fallback message is only shown after the retries are
  exhausted.

### Fix 3 — frontend lot multiplier (smart SL in-between)

- Added a **Lot multiplier** stepper (`×1` = off, up to `×4`) to both
  `command_builder.html` and `frontend/ui.html` with hint
  "Smart SL in-between · x1 = off".
- Commands now emit the `x{mult}` token (e.g. `/m 2440 y 2 0.5 10k x2 M15 4080 --all`).
- The calc box shows the tightened SL level and the multiplied lot size.
- `formatting.py` `trade_usage` already documents the `[x2]` token for both `/m` and `/p`.

---

## 4. Files changed

| File | Change |
|------|--------|
| `src/crackedalert/ctrader/trading.py` | Fill map (`note_fill`/`filled_position_id`), hardened `confirm_position` (orderId→positionId, digits tolerance, 3 retries) |
| `src/crackedalert/main.py` | `PT_EXECUTION_EVENT` → `on_execution` → `note_fill` wiring; `on_entry_hit` confirm flow |
| `src/crackedalert/bot/handlers.py` | Market path creates TP/SL alerts immediately + fast-path cc guard; pending keeps entry-alert flow |
| `src/crackedalert/bot/formatting.py` | `sl_label`/mult surfacing in order-success copy (usage template updated) |
| `src/crackedalert/risk.py` | Mult math surfaced (SL tightening + lot multiplication) |
| `command_builder.html` | Lot multiplier stepper + calc box + `x{mult}` in command |
| `frontend/ui.html` | Same multiplier control for the served UI |
| `tests/test_trading.py` | New `ConfirmPositionTests` (mapping, tolerance, retry) |
| `tests/test_alerts.py` | New `HandlerTpSlTests` (market TP/SL immediate, pending after fill) |
| `tests/test_frontend_contract.py` | Mult contract coverage for both builders |

10 files changed, **+1515 insertions**.

---

## 5. Verification

- New + existing tests: **206 / 207 pass** on the final pushed tree
  (`python -m unittest discover -s tests`).
- The single failure (`test_deploy_contract.test_caddy_ui_block_exists`) is
  **pre-existing and unrelated** — it asserts the old hardcoded domain
  `alert.hotland3x3.my.id`, but commit `9a78d34` intentionally sanitized
  `deploy/caddy-ui.caddyfile` to `YOUR_DOMAIN`. No fix-set file touches it.
- Re-verified the pushed code in context:
  - `trading.py:200-290` — fill map + retries + digits tolerance.
  - `main.py:146-162` — `PT_EXECUTION_EVENT` → `trader.note_fill`.
  - `handlers.py` — market TP/SL immediate + fast-path guard.
  - Both frontends — mult stepper emitting `x{mult}`.

---

## 6. Shipping & deployment

- Commit `3a70fa2` was rebased cleanly onto the remote tip (`9a78d34`) and pushed
  to `main`.
- **Backend changed** (`src/crackedalert/` is the Python bot) → deployment is required.

**On the VPS, run:**

```bash
sudo ./deploy_v2.sh
```

This pulls `main`, reinstalls the package into `/opt/crackedalert/venv`, runs the
smoke test, restarts `cracked-bot`, and copies the new static UI (with the mult
control) to `/var/www/crackedalert-ui`.