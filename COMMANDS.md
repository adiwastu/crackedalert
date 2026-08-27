# Cracked Alert — Complete Command Reference

This document is the exhaustive reference for **everything** the Cracked Alert system can do. It covers:

- **Section 1** — v2 Telegram bot commands (the primary, current stack)
- **Section 2** — v2 CLI, setup, and environment variables
- **Section 3** — v2 deployment & operations (deploy scripts, systemd, bin helpers)
- **Section 4** — Running the test suite

---

## Section 1 — v2 Telegram Bot Commands (PRIMARY)

The v2 bot is a Python + cTrader Open API bot. It connects to cTrader over WebSocket, streams live spot prices, and exposes a Telegram bot. All commands are sent as Telegram messages to the bot.

### Access Control

| Command | Gated? | Description |
|---|---|---|
| `/subscribe` | **No** | Anyone can call. Adds the chat to the allow-list. |
| `/unsubscribe` | **No** | Anyone can call. Removes the chat from the allow-list. |
| All other commands | **Yes** | Only chats in `ALLOWED_CHAT_IDS` (env) or the dynamic subscription store can use them. Unauthorized chats are logged and ignored. |

---

### `/m` — Market Order

Places a **market** order (fills at the live bid/ask). Direction is inferred automatically from the SL relative to the live mid-price.

**Syntax:**
```
/m <sl> <widen:y|n> <rr> <risk%|$amount> <account> [--smart-sl <price> <tf>] [<tf> <guard_price>]
```

**Parameters:**

| # | Parameter | Type | Required | Description |
|---|---|---|---|---|
| 1 | `sl` | float | ✅ | Stop-loss price. Direction is inferred: `sl < mid` → BUY, `sl > mid` → SELL. |
| 2 | `widen` | `y` / `n` | ✅ | `y` = widen SL by 1.00 price unit (10 pips on XAUUSD), labeled "tambah 10 pips". `n` = no widening. |
| 3 | `rr` | float | ✅ | Risk:reward ratio. Must be positive. TP = entry ± (distance × rr). |
| 4 | `risk` | float **or** `$float` | ✅ | **Percent mode:** `0.5` = risk 0.5% of account balance. **Dollar mode:** `$50` = risk exactly $50 (percent is ignored). Must be positive. |
| 5 | `account` | string | ✅ | Account shortcode (e.g. `5k`, `10k`, `raven`, `demo`, `live100k`). Must exist in `CTRADER_ACCOUNTS`. |
| 6 | `tf` | string | ⬜ | Optional CC-guard timeframe. Must be one of: `M1 M5 M15 M30 H1 H4 D1 W1 MN1`. |
| 7 | `guard_price` | float | ⬜ | Optional CC-guard price. Only valid if `tf` is also given. |

**Parameter variations:**

- **Risk as percent:** `/m 2440.00 y 2 0.5 10k`
- **Risk as dollar amount:** `/m 2440.00 y 2 $50 10k`
- **With CC guard:** `/m 2440.00 y 2 0.5 10k M15 4080`
- **Without widen:** `/m 2440.00 n 2 0.5 10k`

**Examples:**
```
/m 2440.00 y 2 0.5 10k
/m 2440.00 y 2 $50 10k
/m 2440.00 y 2 0.5 10k M15 4080
/m 2440.00 n 3 1 5k
```

**Behavior:**
- Fetches live bid/ask for the trade symbol (default `XAUUSD`).
- Fetches account balance.
- Computes lots from risk, distance, and contract size (XAUUSD = 100 oz/lot).
- Lots are **floored** to the broker's volume step so the stated risk % is never exceeded.
- Rejects orders below the broker's minimum lot size.
- Places the order with SL/TP and label `crackedalert`.
- **`--smart-sl <price> <tf>`** (soft candle-close stop): does NOT move the broker-side SL. Arms a guard that closes the position when a `<tf>` candle CLOSES past `<price>` (BUY: below, SELL: above). `<tf>` must be one of `M1 M5 M15 M30 H1`. `<price>` must sit between the fill and the original SL. Lots stay anchored to the original SL; the reply reports the estimated exposure at the smart level.
- SL/TP are real broker-side orders -- results (fills, TP/SL hits) are visible in the cTrader app.
- If a CC guard (`tf` + `guard_price`) is given, creates a candle-close guard that auto-closes the position when a candle closes past the guard price.

**Error cases:**
- `error: account '<acct>' not found.` — unknown account shortcode.
- `error: live trading is locked until the Phase 5 cutover. use the demo account.` — live account while `LIVE_TRADING_ENABLED` is off.
- `error: cTrader <env> link is down, try again shortly.` — connection down.
- `error: could not fetch live price for <symbol>.` — no quote.
- `error: could not fetch balance for <acct> (cTrader error <code>: <desc>).` — the cTrader balance request failed; the code/description reveal why. Also logged to the service journal.
- `error: could not fetch balance for <acct> (balance <n>).` — the account reports a zero/negative balance.
- `error: lot size calculated to 0. check parameters.` — math produced 0 lots.
- `error: calculated <n> lots below the <min> minimum -- not placing the order.` — below broker minimum.

---

### `/p` — Pending Order

Places a **pending** order (limit or stop) at an explicit entry price. The placement price is spread-offset (BUY above, SELL below). LIMIT vs STOP is inferred from where the placement price sits relative to the current book.

**Syntax:**
```
/p <entry> <sl> <widen:y|n> <rr> <risk%|$amount> <account> [<tf> <guard_price>]
```

**Parameters:**

| # | Parameter | Type | Required | Description |
|---|---|---|---|---|
| 1 | `entry` | float | ✅ | Desired entry price. Direction is inferred: `sl < entry` → BUY, `sl > entry` → SELL. |
| 2 | `sl` | float | ✅ | Stop-loss price. |
| 3 | `widen` | `y` / `n` | ✅ | Same as `/m`. |
| 4 | `rr` | float | ✅ | Risk:reward ratio. Must be positive. |
| 5 | `risk` | float **or** `$float` | ✅ | Percent or dollar risk (same as `/m`). |
| 6 | `account` | string | ✅ | Account shortcode. |
| 7 | `tf` | string | ⬜ | Optional CC-guard timeframe. |
| 8 | `guard_price` | float | ⬜ | Optional CC-guard price. |

**Parameter variations:**

- **Risk as percent:** `/p 2450.00 2455.00 n 3 1 5k`
- **Risk as dollar amount:** `/p 2450.00 2455.00 n 3 $100 5k`
- **With CC guard:** `/p 2450.00 2455.00 n 3 1 5k H1 2445`
- **With soft candle-close stop:** `/p 2450.00 2455.00 n 3 1 5k --smart-sl 2451.25 M15`
- **Without widen:** `/p 2450.00 2455.00 n 3 1 5k`

**Examples:**
```
/p 2450.00 2455.00 n 3 1 5k
/p 2450.00 2455.00 n 3 $100 5k
/p 2450.00 2455.00 n 3 1 5k H1 2445
/p 2450.00 2455.00 y 2 0.5 10k
```

**Behavior:**
- Fetches live bid/ask and account balance.
- Computes the spread-offset placement price.
- Infers LIMIT or STOP order type.
- SL/TP/dist/lots are computed off the **placement price** (the actual fill), not the raw entry.
- Places the order with `GOOD_TILL_CANCEL` time-in-force.
- On success with a CC guard or `--smart-sl` requested, replies with a "CC guard queued" message — the guard activates automatically when the order fills.

**Error cases:** Same as `/m`.

---

### `/be` — Move SL to Breakeven

Moves the stop-loss to breakeven + spread buffer on **every** open position on the account.

**Syntax:**
```
/be <account>
```

**Parameters:**

| # | Parameter | Type | Required | Description |
|---|---|---|---|---|
| 1 | `account` | string | ✅ | Account shortcode. |

**Examples:**
```
/be live100k
/be 5k
```

**Behavior:**
- For each open position: `BE_SL = entry + spread` (BUY) or `entry - spread` (SELL).
- Only amends when the market has actually moved past the BE level (BUY: bid ≥ BE_SL; SELL: ask ≤ BE_SL).
- Existing TP is preserved.
- Reports per-position results: `breakeven set`, `not in profit by spread yet`, or an error.

---

### `/close` — Close a Single Position

Closes one open position at its full volume.

**Syntax:**
```
/close <position_id> <account>
```

**Parameters:**

| # | Parameter | Type | Required | Description |
|---|---|---|---|---|
| 1 | `position_id` | integer | ✅ | The position ID (from `/positions`). |
| 2 | `account` | string | ✅ | Account shortcode. |

**Examples:**
```
/close 4467051 live100k
/close 123456 5k
```

**Behavior:**
- Closes the position at full volume.
- Cancels any CC guards tied to that position.

**Error cases:**
- `error: position <id> not found on <acct>.` — position doesn't exist.
- `error: position <id> has no volume to close.` — zero volume.

---

### `/close_all` — Close All Positions

Closes **every** open position on the account.

**Syntax:**
```
/close_all <account>
```

**Parameters:**

| # | Parameter | Type | Required | Description |
|---|---|---|---|---|
| 1 | `account` | string | ✅ | Account shortcode. |

**Examples:**
```
/close_all live100k
/close_all 10k
```

**Behavior:**
- Closes all positions, reporting per-position success/failure.
- Cancels CC guards for every closed position.

---

### `/cancel_order` — Cancel a Pending Order

Cancels one working (pending) order.

**Syntax:**
```
/cancel_order <order_id> <account>
```

**Parameters:**

| # | Parameter | Type | Required | Description |
|---|---|---|---|---|
| 1 | `order_id` | integer | ✅ | The order ID (from `/orders`). |
| 2 | `account` | string | ✅ | Account shortcode. |

**Examples:**
```
/cancel_order 4467051 live100k
/cancel_order 123456 5k
```

**Behavior:**
- Cancels the pending order at the broker.

---

### `/positions` — List Open Positions

Lists all open positions for an account.

**Syntax:**
```
/positions <account>
```

**Parameters:**

| # | Parameter | Type | Required | Description |
|---|---|---|---|---|
| 1 | `account` | string | ✅ | Account shortcode. |

**Examples:**
```
/positions live100k
/positions 5k
```

**Behavior:**
- Fetches open positions via cTrader reconcile.
- Shows per position: side, symbol, volume (lots), entry price, SL, TP, swap, and live current price.

---

### `/orders` — List Working Orders

Lists all working (pending) orders for an account.

**Syntax:**
```
/orders <account>
```

**Parameters:**

| # | Parameter | Type | Required | Description |
|---|---|---|---|---|
| 1 | `account` | string | ✅ | Account shortcode. |

**Examples:**
```
/orders live100k
/orders 10k
```

**Behavior:**
- Fetches working orders via cTrader reconcile.
- Shows per order: side, symbol, volume (lots), price, SL, TP, and order type.

---

### `/alert` — Set a Price Alert

Sets a manual price alert. Fires when the live mid-price crosses the target.

**Syntax:**
```
/alert <target> [symbol] [notes...]
```

**Parameters:**

| # | Parameter | Type | Required | Description |
|---|---|---|---|---|
| 1 | `target` | float | ✅ | Target price. |
| 2 | `symbol` | string | ⬜ | Symbol (e.g. `XAUUSD`). Defaults to the trade symbol. Only accepted if it looks like a symbol (ALL-CAPS or a known symbol on the account). |
| 3 | `notes...` | string | ⬜ | Free-text notes. Defaults to `Price target reached.` |

**Parameter variations:**

- **Default symbol:** `/alert 2450.00 approaching demand`
- **Explicit symbol:** `/alert 2450.00 XAUUSD approaching demand`
- **No notes:** `/alert 2450.00`

**Examples:**
```
/alert 2450.00 approaching demand
/alert 2450.00 XAUUSD approaching demand
/alert 2450.00
```

**Behavior:**
- Fetches the live price to determine direction: live < target → `CROSSING_UP` (fires when price rises to target); live > target → `CROSSING_DOWN` (fires when price falls to target).
- Stores the alert in SQLite.
- Fires once when crossed, notifies the owning chat, then deletes itself.

---

### `/ccalert` — Set a Candle-Close Alert

Sets a candle-close alert that fires when a **closed candle** crosses the target.

**Syntax:**
```
/ccalert <tf> <price> <above|below> [symbol] [notes...]
```

**Parameters:**

| # | Parameter | Type | Required | Description |
|---|---|---|---|---|
| 1 | `tf` | string | ✅ | Timeframe. Must be one of: `M1 M5 M15 M30 H1 H4 D1 W1 MN1`. |
| 2 | `price` | float | ✅ | Target price. |
| 3 | `direction` | `above` / `below` | ✅ | `above` = fires when a candle closes ≥ price. `below` = fires when a candle closes ≤ price. |
| 4 | `symbol` | string | ⬜ | Symbol. Defaults to the trade symbol. Only accepted if ALL-CAPS. |
| 5 | `notes...` | string | ⬜ | Free-text notes. Defaults to `timeframe candle target reached.` |

**Parameter variations:**

- **Default symbol:** `/ccalert M15 2450 above breakout`
- **Explicit symbol:** `/ccalert M15 2450 above XAUUSD breakout`
- **Below direction:** `/ccalert H1 2400 below support`

**Examples:**
```
/ccalert M15 2450 above XAUUSD breakout
/ccalert M15 2450 above breakout
/ccalert H1 2400 below support
```

**Behavior:**
- Fetches the latest closed-bar close to confirm the symbol/timeframe is available.
- Stores the alert in SQLite.
- The candle feed polls every 10 seconds; when a new bar closes and crosses the target, it fires and deletes itself.

---

### `/list` — List Active Alerts

Lists all active manual alerts for the calling chat.

**Syntax:**
```
/list
```

**Examples:**
```
/list
```

**Behavior:**
- Shows each alert as `(ID) SYMBOL @ TARGET - notes`.
- If none: `no active alerts.`

---

### `/cancel` — Cancel an Alert

Deletes an alert owned by the calling chat.

**Syntax:**
```
/cancel <id>
```

**Parameters:**

| # | Parameter | Type | Required | Description |
|---|---|---|---|---|
| 1 | `id` | string | ✅ | The 4-character alert ID (from `/list`). |

**Examples:**
```
/cancel 42O4
/cancel A1B2
```

**Behavior:**
- Deletes manual price alerts owned by this chat.
- Ownership is enforced: another subscriber's alerts are not visible to `/cancel`.
- Success: `alert <ID> cancelled.`
- Not found: `id <ID> not found or doesn't belong to you.`

---

### `/cclist` — List Active Candle Alerts

Lists all active candle-close alerts for the calling chat.

**Syntax:**
```
/cclist
```

**Examples:**
```
/cclist
```

**Behavior:**
- Shows each alert as `(ID) [guard #N] SYMBOL TF close DIRECTION @ TARGET - notes`.
- CC guards show `[guard #position_id]`.
- If none: `no active candle alerts.`

---

### `/cccancel` — Cancel a Candle Alert

Deletes a candle-close alert owned by the calling chat.

**Syntax:**
```
/cccancel <id>
```

**Parameters:**

| # | Parameter | Type | Required | Description |
|---|---|---|---|---|
| 1 | `id` | string | ✅ | The alert ID (from `/cclist`). |

**Examples:**
```
/cccancel 3
/cccancel A1B2
```

**Behavior:**
- Success: `candle alert <ID> cancelled.`
- Not found: `id <ID> not found or doesn't belong to you.`

---

### `/help` — Show Help

Shows the command-builder UI link and the Android alarm-app APK download.

**Syntax:**
```
/help
```

**Examples:**
```
/help
```

**Behavior:**
- Replies with the frontend UI link (`alert.hotland3x3.my.id/ui.html`).
- Replies with the Android app APK download link (GitHub raw).

---

### `/subscribe` — Allow This Chat

Adds the calling chat to the dynamic allow-list. **Not gated** — anyone can call.

**Syntax:**
```
/subscribe
```

**Examples:**
```
/subscribe
```

**Behavior:**
- Success (new): `✅ chat <id> subscribed.`
- Already subscribed: `chat <id> was already subscribed.`

---

### `/unsubscribe` — Remove This Chat

Removes the calling chat from the dynamic allow-list. **Not gated** — anyone can call.

**Syntax:**
```
/unsubscribe
```

**Examples:**
```
/unsubscribe
```

**Behavior:**
- Success: `✅ chat <id> unsubscribed.`
- Not subscribed: `chat <id> was not subscribed.`

---

### Trade Results & CC Guards

Since v2.0.31 the bot no longer generates internal trade alerts (`entry`/`tp`/`sl`). Fills and TP/SL executions arrive as cTrader push events; results are visible in the cTrader app. Broker-side SL/TP on every order are unaffected.

**CC guards** (candle-close position auto-close) remain fully supported:
- Created immediately for `/m` used with a `tf` + `guard_price`.
- Queued for `/p` orders and activated automatically when the order fills.
- BUY positions: guard fires if a candle closes **below** the guard price.
- SELL positions: guard fires if a candle closes **above** the guard price.
- On fire, the position is auto-closed and all subscribers are notified.
- If the position is already gone (SL/TP hit), the guard is removed with a notice.

---|---|---|
| `entry` | Entry price is hit | Confirms the position exists, then creates TP + SL alerts. Broadcast to all subscribers. |
| `tp` | Take-profit price is hit | Broadcast to all subscribers, then deleted. |
| `sl` | Stop-loss price is hit | Broadcast to all subscribers, then deleted. |

**CC guards** (candle-close position auto-close):
- Created automatically when `/m` is used with a `tf` + `guard_price`.
- Queued for `/p` orders and activated when the order fills.
- BUY positions: guard fires if a candle closes **below** the guard price.
- SELL positions: guard fires if a candle closes **above** the guard price.
- On fire, the position is auto-closed and all subscribers are notified.
- If the position is already gone (SL/TP hit), the guard is removed with a notice.

---

## Section 2 — v2 CLI, Setup, and Environment

### CLI

```
python -m crackedalert
```

Runs the bot (alerts live, trading enabled).

**Flags:**

| Flag | Description |
|---|---|
| `--smoke` | Connect, authenticate every account, print balances, then exit. Returns exit code 0 on success, 1 on any failure. |
| `-v`, `--verbose` | Keep third-party HTTP/websocket logs (noisy; prints the bot token in Telegram URLs). |

**Examples:**
```
python -m crackedalert
python -m crackedalert --smoke
python -m crackedalert -v
python -m crackedalert --smoke -v
```

The installed console script is also available as `crackedalert` (from `pyproject.toml`):
```
crackedalert --smoke
```

### `auth_setup.py` — One-Time OAuth Setup

Interactive script that walks through the cTrader Open API authorization flow.

```
python auth_setup.py
```

**Interactive prompts:**

| Prompt | Description |
|---|---|
| `Client ID` | From the cTrader Open API portal (Applications → Credentials → View). |
| `Client Secret (hidden)` | From the same portal. |
| `Redirect URI` | Must match the registered redirect URI. Default: `https://hotland3x3.my.id/sable/callback`. |

**Flow:**
1. Prints an authorize URL — open it in a browser, log in, approve.
2. Browser lands on the redirect URI with `?code=...` — paste the full URL or just the code.
3. Exchanges the code for access + refresh tokens.
4. Writes `tokens.json` next to the script (mode 0600).
5. Connects to cTrader and lists every trading account the token can see (copy the ids into `CTRADER_ACCOUNTS`).

**Output example:**
```
  ctidTraderAccountId   login        environment
  1234567890            12345678     live
  9876543210            87654321     demo
```

### Environment Variables

Loaded from `/etc/cracked_alert/.env_cracked` (production) or `./.env` (local dev). Process env wins.

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Telegram bot token. |
| `ALLOWED_CHAT_IDS` | ✅ | Comma-separated Telegram chat IDs allowed to command the bot. |
| `CTRADER_CLIENT_ID` | ✅ | cTrader Open API client ID. |
| `CTRADER_CLIENT_SECRET` | ✅ | cTrader Open API client secret. |
| `CTRADER_ACCOUNTS` | ✅ | JSON mapping shortcode → `{id, env}`. `env` must be `live` or `demo`. |
| `PRICE_FEED_ACCOUNT` | ⬜ | Which account's price feed drives alerts. Default: `demo`. Must be a configured shortcode. |
| `CRACKED_DATA_DIR` | ⬜ | Data directory for `tokens.json` + `cracked.db`. Default: `/etc/cracked_alert`. |
| `TRADE_SYMBOL` | ⬜ | The symbol traded by `/m` and `/p`. Default: `XAUUSD`. |

**`CTRADER_ACCOUNTS` example:**
```json
{"5k":{"id":123,"env":"live"},"10k":{"id":456,"env":"live"},"raven":{"id":789,"env":"live"},"demo":{"id":321,"env":"demo"},"live100k":{"id":654,"env":"live"}}
```

**Data files (in `CRACKED_DATA_DIR`):**

| File | Description |
|---|---|
| `tokens.json` | OAuth access + refresh tokens (created by `auth_setup.py`). |
| `cracked.db` | SQLite database: `alerts`, `candle_alerts`, `allowed_chats` tables. |
| `cracked_alerts.tsv` | Legacy v1 alert file. Auto-imported into SQLite on first v2 startup, then renamed to `.imported`. |

---

## Section 3 — v2 Deployment & Operations

### `deploy_v2.sh` — Full Deployment

Deploys the v2 bot to a VPS. Must run as root.

```
sudo ./deploy_v2.sh
```

**Steps performed:**
1. `git pull origin main`
2. Ensures `/etc/cracked_alert` (mode 700). Warns if `.env_cracked` or `tokens.json` are missing.
3. Creates `/opt/crackedalert/venv` if needed, installs the package.
4. Installs `bin/run_bot.sh` → `/usr/local/bin/crackedalert-run` and the systemd unit.
5. Runs `python -m crackedalert --smoke`. If it passes, enables + restarts `cracked-bot.service`. If it fails, the service is **not** restarted and the script exits 1.
6. (Cleanup block) Best-effort disables leftover legacy services if present.

### `bin/run_bot.sh` — Bot Launcher

Launches the v2 bot with whichever venv exists. Shared by systemd; also usable for manual foreground runs.

```
bin/run_bot.sh [args...]
```

- Picks `/opt/crackedalert/venv/bin/python` or `/opt/crackedalert-dev/bin/python`.
- Override with `CRACKED_PYTHON=/path/to/bin/python`.
- Execs `python -m crackedalert "$@"`.

**Examples:**
```
bin/run_bot.sh
bin/run_bot.sh --smoke
CRACKED_PYTHON=/opt/crackedalert/venv/bin/python bin/run_bot.sh
```

### systemd — `cracked-bot.service`

The v2 bot runs as a systemd service.

```
systemctl start cracked-bot
systemctl stop cracked-bot
systemctl restart cracked-bot
systemctl status cracked-bot
systemctl enable cracked-bot
systemctl disable cracked-bot
journalctl -u cracked-bot -f
journalctl -u cracked-bot -n 100 --no-pager
```

**Unit details:**
- `ExecStart=/usr/local/bin/crackedalert-run`
- `Restart=always`, `RestartSec=5`
- `After=network-online.target`, `Wants=network-online.target`

---

## Section 4 — Running the Test Suite

The project has a pytest test suite covering alerts, candles, client, formatting, parsing, risk, and trading.

```
python -m pytest tests/ -v
```

Or run individual test files:

```
python -m pytest tests/test_alerts.py -v
python -m pytest tests/test_candles.py -v
python -m pytest tests/test_client.py -v
python -m pytest tests/test_formatting.py -v
python -m pytest tests/test_parsing.py -v
python -m pytest tests/test_risk.py -v
python -m pytest tests/test_trading.py -v
```

---

## Quick Reference — All v2 Telegram Commands

| Command | Syntax | Purpose |
|---|---|---|
| `/m` | `/m <sl> <widen> <rr> <risk> <account> [tf guard]` | Market order |
| `/p` | `/p <entry> <sl> <widen> <rr> <risk> <account> [tf guard]` | Pending order |
| `/be` | `/be <account>` | Move SL to breakeven |
| `/close` | `/close <id> <account>` | Close one position |
| `/close_all` | `/close_all <account>` | Close all positions |
| `/cancel_order` | `/cancel_order <id> <account>` | Cancel a pending order |
| `/positions` | `/positions <account>` | List open positions |
| `/orders` | `/orders <account>` | List working orders |
| `/alert` | `/alert <target> [symbol] [notes]` | Set a price alert |
| `/ccalert` | `/ccalert <tf> <price> <above\|below> [symbol] [notes]` | Candle-close alert |
| `/list` | `/list` | List active alerts |
| `/cancel` | `/cancel <id>` | Cancel an alert |
| `/cclist` | `/cclist` | List candle alerts |
| `/cccancel` | `/cccancel <id>` | Cancel a candle alert |
| `/help` | `/help` | Show help |
| `/subscribe` | `/subscribe` | Allow this chat |
| `/unsubscribe` | `/unsubscribe` | Remove this chat |

## Quick Reference — All v2 CLI / Ops Commands

| Command | Purpose |
|---|---|
| `python -m crackedalert` | Run the bot |
| `python -m crackedalert --smoke` | Smoke test (connect + auth + balances) |
| `python -m crackedalert -v` | Run with verbose logs |
| `python auth_setup.py` | One-time OAuth setup |
| `sudo ./deploy_v2.sh` | Deploy v2 to VPS |
| `systemctl start/stop/restart/status cracked-bot` | Manage v2 service |
| `journalctl -u cracked-bot -f` | Follow v2 logs |