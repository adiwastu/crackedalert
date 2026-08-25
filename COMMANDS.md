# Cracked Alert — Complete Command Reference

This document is the exhaustive reference for **everything** the Cracked Alert system can do. It covers:

- **Section 1** — v2 Telegram bot commands (the primary, current stack)
- **Section 2** — v2 CLI, setup, and environment variables
- **Section 3** — v2 deployment & operations (deploy scripts, systemd, bin helpers)
- **Section 4** — MT5 Flask REST API (all endpoints)
- **Section 5** — Legacy v1 bash/MT5 stack (**DEPRECATED / RETIRED**)
- **Section 6** — Running the test suite

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
/m <sl> <widen:y|n> <rr> <risk%|$amount> <account> [<tf> <guard_price>]
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
- On success, creates an **auto entry alert** that fires when the entry is hit, confirms the position, then creates **auto TP and SL alerts** broadcast to all subscribers.
- If a CC guard (`tf` + `guard_price`) is given, creates a candle-close guard that auto-closes the position when a candle closes past the guard price.

**Error cases:**
- `error: account '<acct>' not found.` — unknown account shortcode.
- `error: live trading is locked until the Phase 5 cutover. use the demo account.` — live account while `LIVE_TRADING_ENABLED` is off.
- `error: cTrader <env> link is down, try again shortly.` — connection down.
- `error: could not fetch live price for <symbol>.` — no quote.
- `error: could not fetch balance for <acct> (cTrader error <code>: <desc>).` — the cTrader balance request failed; the code/description reveal why (e.g. expired demo account or invalid token). Also logged to the service journal.
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
- On success, creates an **auto entry alert** with the CC-guard params stored on it. When the entry fills, the guard is created automatically.
- If a CC guard was requested, replies with a "CC guard queued" message — the guard activates when the order fills.

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
- Cancels any auto TP/SL alerts tied to that position.
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
- Cancels auto TP/SL alerts and CC guards for every closed position.

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
- Cancels the pending order.
- Cancels the auto entry alert tied to that order.

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
- Shows each alert as `(ID) [tag] SYMBOL @ TARGET - notes`.
- Tags: `[entry]`, `[TP]`, `[SL]` for auto trade alerts; no tag for manual alerts.
- If none: `no active alerts.`

---

### `/cancel` — Cancel an Alert

Deletes a manual alert owned by the calling chat.

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
- Only deletes **manual** alerts owned by this chat. Auto trade alerts are system-managed and not user-cancellable.
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

### Auto Trade Alerts (system-generated, not user commands)

When a trade is placed via `/m` or `/p`, the bot automatically creates internal alerts:

| Kind | Fires when | Behavior |
|---|---|---|
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
6. (Cutover block) Disables the legacy `cracked-listener` and `cracked-checker` services.

### `bin/run_bot.sh` — Bot Launcher

Launches the v2 bot with whichever venv exists. Shared by systemd and `bin/use_new.sh`.

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

### `bin/use_new.sh` — Switch to the New Stack

Stops the bash services and any stray bot, then runs the new bot. Must run as root.

```
sudo bin/use_new.sh
sudo bin/use_new.sh --service
```

**Without `--service`:** runs the bot in the **foreground** (Ctrl+C stops it).

**With `--service`:** runs via systemd in the background. On first run, installs the launcher + unit, then starts `cracked-bot.service`.

**Steps performed:**
1. Stops `cracked-listener.service` and `cracked-checker.service`.
2. Stops `cracked-bot.service` and kills any foreground `python -m crackedalert` process.
3. Waits 2 seconds (lets Telegram release the long-poll to avoid 409 Conflict).
4. Aborts if the old services refused to stop.
5. Starts the new bot (foreground or `--service`).

### `bin/use_old.sh` — Switch to the Old Stack

Stops the new Python bot and starts the legacy bash services. Must run as root.

```
sudo bin/use_old.sh
```

**Steps performed:**
1. Stops `cracked-bot.service` and kills any foreground `python -m crackedalert`.
2. Waits 2 seconds.
3. Starts `cracked-listener.service` and `cracked-checker.service`.
4. Reports status of each service.
5. Warns if alerts live in the v2 SQLite db (the bash checker sees an empty TSV).

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

## Section 4 — MT5 Flask REST API

The MT5 Flask API (the v1 backend, still referenced by the legacy stack) exposes the following endpoints. All require an `Authorization` header (API key). Base URLs used by the legacy stack:

| Account | Base URL |
|---|---|
| `5k` | `https://api.hotland3x3.my.id` |
| `10k` | `https://api-5ers.hotland3x3.my.id` |
| `raven` | `https://api-raven.hotland3x3.my.id` |
| `demo` | `https://api-demo.hotland3x3.my.id` |

---

### `GET /health`

Health check for the app and MT5 connection.

**Response:**
```json
{
  "status": "ok",
  "mt5_initialized": true,
  "mt5_connected": true
}
```

**Example:**
```bash
curl -H "Authorization: <key>" https://api.hotland3x3.my.id/health
```

---

### `GET /account`

Returns the current financial state of the trading account.

**Response fields:** `balance`, `equity`, `margin`, `margin_free`, `margin_level`, `profit`, `currency`, `leverage`, `login`, `name`, `company`, `server`.

**Example:**
```bash
curl -H "Authorization: <key>" https://api.hotland3x3.my.id/account
```

---

### `POST /order`

Executes a **market order** (no `price`) or a **smart pending order** (with `price`).

**Body parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | ✅ | Symbol (e.g. `XAUUSD`). |
| `volume` | number | ✅ | Volume in lots. |
| `type` | string | ✅ | `BUY` or `SELL`. |
| `price` | number | ⬜ | If provided, a pending order (Limit/Stop) is placed automatically. |
| `sl` | number | ⬜ | Stop-loss. |
| `tp` | number | ⬜ | Take-profit. |
| `magic` | integer | ⬜ | Magic number. Default `0`. |
| `deviation` | integer | ⬜ | Deviation. Default `20`. |
| `comment` | string | ⬜ | Order comment. Default `""`. |
| `type_filling` | string | ⬜ | `ORDER_FILLING_IOC`, `ORDER_FILLING_FOK`, or `ORDER_FILLING_RETURN`. |

**Examples:**
```bash
# Market order
curl -X POST https://api.hotland3x3.my.id/order \
  -H "Authorization: <key>" -H "Content-Type: application/json" \
  -d '{"symbol":"XAUUSD","type":"BUY","volume":0.04,"sl":2440.00,"tp":2460.00,"magic":777}'

# Pending order (with price)
curl -X POST https://api.hotland3x3.my.id/order \
  -H "Authorization: <key>" -H "Content-Type: application/json" \
  -d '{"symbol":"XAUUSD","type":"BUY","volume":0.04,"price":2450.00,"sl":2440.00,"tp":2460.00,"magic":777}'
```

---

### `POST /close_position`

Closes a specific trading position.

**Body parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `position` | object | ✅ | Position data. |
| `position.ticket` | integer | ✅ | Position ticket. |
| `position.symbol` | string | ✅ | Symbol. |
| `position.type` | integer | ✅ | Position type. |
| `position.volume` | number | ✅ | Volume. |

**Example:**
```bash
curl -X POST https://api.hotland3x3.my.id/close_position \
  -H "Authorization: <key>" -H "Content-Type: application/json" \
  -d '{"position":{"ticket":4467051,"symbol":"XAUUSD","type":0,"volume":0.04}}'
```

---

### `POST /close_all_positions`

Closes all open positions, optionally filtered.

**Body parameters (all optional):**

| Parameter | Type | Description |
|---|---|---|
| `order_type` | string | `BUY`, `SELL`, or `all` (default `all`). |
| `magic` | integer | Magic number filter. |

**Examples:**
```bash
# Close everything
curl -X POST https://api.hotland3x3.my.id/close_all_positions \
  -H "Authorization: <key>" -H "Content-Type: application/json" \
  -d '{}'

# Close only BUY positions
curl -X POST https://api.hotland3x3.my.id/close_all_positions \
  -H "Authorization: <key>" -H "Content-Type: application/json" \
  -d '{"order_type":"BUY"}'

# Close only positions with magic 777
curl -X POST https://api.hotland3x3.my.id/close_all_positions \
  -H "Authorization: <key>" -H "Content-Type: application/json" \
  -d '{"magic":777}'
```

---

### `POST /modify_sl_tp`

Modifies the Stop Loss and Take Profit for a specific position.

**Body parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `position` | integer | ✅ | Position ticket. |
| `sl` | number | ⬜ | New stop-loss. |
| `tp` | number | ⬜ | New take-profit. |

**Example:**
```bash
curl -X POST https://api.hotland3x3.my.id/modify_sl_tp \
  -H "Authorization: <key>" -H "Content-Type: application/json" \
  -d '{"position":4467051,"sl":2445.00,"tp":2470.00}'
```

---

### `GET /get_positions`

Retrieves all open positions, optionally filtered by magic.

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `magic` | integer | ⬜ | Magic number filter. |

**Response fields per position:** `ticket`, `symbol`, `type`, `volume`, `price_open`, `price_current`, `sl`, `tp`, `profit`, `swap`, `comment`, `magic`, `external_id`, `time`.

**Examples:**
```bash
curl -H "Authorization: <key>" https://api.hotland3x3.my.id/get_positions
curl -H "Authorization: <key>" "https://api.hotland3x3.my.id/get_positions?magic=777"
```

---

### `GET /positions_total`

Retrieves the total number of open positions.

**Response:**
```json
{"total": 3}
```

**Example:**
```bash
curl -H "Authorization: <key>" https://api.hotland3x3.my.id/positions_total
```

---

### `GET /fetch_data_pos`

Retrieves historical price data for a symbol starting from a specific position.

**Query parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `symbol` | string | ✅ | — | Symbol name. |
| `timeframe` | string | ⬜ | `M1` | Timeframe (e.g. `M1`, `M5`, `H1`). |
| `num_bars` | integer | ⬜ | `100` | Number of bars to fetch. |

**Response fields per bar:** `time`, `open`, `high`, `low`, `close`, `tick_volume`, `spread`, `real_volume`.

**Examples:**
```bash
curl -H "Authorization: <key>" "https://api.hotland3x3.my.id/fetch_data_pos?symbol=XAUUSD&timeframe=M1&num_bars=1"
curl -H "Authorization: <key>" "https://api.hotland3x3.my.id/fetch_data_pos?symbol=XAUUSD&timeframe=H1&num_bars=50"
```

---

### `GET /fetch_data_range`

Retrieves historical price data within a date range.

**Query parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `symbol` | string | ✅ | — | Symbol name. |
| `timeframe` | string | ⬜ | `M1` | Timeframe. |
| `start` | datetime (ISO) | ✅ | — | Start datetime. |
| `end` | datetime (ISO) | ✅ | — | End datetime. |

**Example:**
```bash
curl -H "Authorization: <key>" "https://api.hotland3x3.my.id/fetch_data_range?symbol=XAUUSD&timeframe=M1&start=2024-01-01T00:00:00&end=2024-01-02T00:00:00"
```

---

### `GET /symbol_info/{symbol}`

Retrieves detailed information for a symbol.

**Path parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | ✅ | Symbol name. |

**Response fields:** `name`, `description`, `path`, `points`, `price_digits`, `spread`, `trade_mode`, `volume_min`, `volume_max`, `volume_step`.

**Example:**
```bash
curl -H "Authorization: <key>" https://api.hotland3x3.my.id/symbol_info/XAUUSD
```

---

### `GET /symbol_info_tick/{symbol}`

Retrieves the latest tick for a symbol.

**Path parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | ✅ | Symbol name. |

**Response fields:** `time`, `bid`, `ask`, `last`, `volume`.

**Example:**
```bash
curl -H "Authorization: <key>" https://api.hotland3x3.my.id/symbol_info_tick/XAUUSD
```

---

### `GET /get_deal_from_ticket`

Retrieves deal information for a ticket.

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `ticket` | integer | ✅ | Ticket number. |

**Response fields:** `ticket`, `type`, `symbol`, `volume`, `open_price`, `close_price`, `open_time`, `close_time`, `profit`, `swap`, `commission`, `comment`.

**Example:**
```bash
curl -H "Authorization: <key>" "https://api.hotland3x3.my.id/get_deal_from_ticket?ticket=4467051"
```

---

### `GET /get_order_from_ticket`

Retrieves order information for a ticket.

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `ticket` | integer | ✅ | Ticket number. |

**Response:** `{"order": {...}}`

**Example:**
```bash
curl -H "Authorization: <key>" "https://api.hotland3x3.my.id/get_order_from_ticket?ticket=4467051"
```

---

### `GET /history_deals_get`

Retrieves historical deals within a date range for a position.

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `from_date` | datetime (ISO) | ✅ | Start date. |
| `to_date` | datetime (ISO) | ✅ | End date. |
| `position` | integer | ✅ | Position number. |

**Example:**
```bash
curl -H "Authorization: <key>" "https://api.hotland3x3.my.id/history_deals_get?from_date=2024-01-01T00:00:00&to_date=2024-01-02T00:00:00&position=4467051"
```

---

### `GET /history_orders_get`

Retrieves historical orders for a ticket.

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `ticket` | integer | ✅ | Ticket number. |

**Example:**
```bash
curl -H "Authorization: <key>" "https://api.hotland3x3.my.id/history_orders_get?ticket=4467051"
```

---

### `GET /last_error`

Retrieves the last MT5 error code and message.

**Response:**
```json
{"error_code": 0, "error_message": "no error"}
```

**Example:**
```bash
curl -H "Authorization: <key>" https://api.hotland3x3.my.id/last_error
```

---

### `GET /last_error_str`

Retrieves the last MT5 error message string.

**Response:**
```json
{"error_message": "no error"}
```

**Example:**
```bash
curl -H "Authorization: <key>" https://api.hotland3x3.my.id/last_error_str
```

---

## Section 5 — Legacy v1 Bash/MT5 Stack (DEPRECATED / RETIRED)

> ⚠️ **DEPRECATED.** This is the old bash + MT5 stack. It has been replaced by the v2 Python + cTrader bot. It is documented here for completeness only. The v2 bot auto-imports the legacy alert TSV into SQLite on first startup.

### Legacy Telegram Commands

The legacy listener polls Telegram directly and routes commands. All commands are XAUUSD-only.

| Command | Syntax | Description |
|---|---|---|
| `/m` | `/m <sl> <widen:y/n> <rr> <risk%> <account>` | Market order. |
| `/p` | `/p <entry> <sl> <widen:y/n> <rr> <risk%> <account>` | Pending order. |
| `/alert` | `/alert <price> [symbol] [message]` | Set a price alert. |
| `/list` | `/list` | List active alerts. |
| `/cancel` | `/cancel <id>` | Cancel an alert. |
| `/help` | `/help` | Show help. |

**Legacy accounts:** `5k`, `10k`, `raven`, `demo` (mapped to the MT5 API base URLs).

**Legacy `/m` example:**
```
/m 2440.00 y 2 0.5 10k
```

**Legacy `/p` example:**
```
/p 2450.00 2455.00 n 3 1 5k
```

**Legacy `/alert` example:**
```
/alert 2450.00 XAUUSD Approaching support
```

### `deploy.sh` — Legacy Deployment

Deploys the legacy bash stack. Must run as root.

```
sudo ./deploy.sh
```

**Steps performed:**
1. `git pull origin main`
2. Creates `/etc/cracked_alert`, touches `cracked_alerts.tsv` and `.tg_offset`, chmod 700.
3. Installs `bin/cracked_listener.sh` and `bin/cracked_checker.sh` to `/usr/local/bin/`.
4. Installs `systemd/cracked-listener.service` and `systemd/cracked-checker.service`.
5. Enables + restarts both services.

### `bin/cracked_listener.sh` — Legacy Telegram Listener

Long-polls Telegram for commands, routes them, and executes orders via the MT5 API.

- Reads offset from `/etc/cracked_alert/.tg_offset`.
- Fetches updates with a 100-second timeout.
- Routes `/m`, `/p`, `/alert`, `/list`, `/cancel`, `/help`.
- Uses `awk` as an embedded math engine for lot/TP/SL calculations.
- Sends results via Telegram.

### `bin/cracked_checker.sh` — Legacy Watchdog Checker

Polls the MT5 API every 5 seconds and fires alerts.

- Reads `/etc/cracked_alert/cracked_alerts.tsv`.
- For each unique symbol, fetches the latest M1 close.
- Evaluates each alert: `CROSSING_UP` fires when live ≥ target; `CROSSING_DOWN` fires when live ≤ target.
- Sends the alert via Telegram, then deletes the row from the TSV.

### Legacy systemd services

```
systemctl start cracked-listener
systemctl stop cracked-listener
systemctl restart cracked-listener
systemctl status cracked-listener
journalctl -u cracked-listener -f

systemctl start cracked-checker
systemctl stop cracked-checker
systemctl restart cracked-checker
systemctl status cracked-checker
journalctl -u cracked-checker -f
```

---

## Section 6 — Running the Test Suite

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
| `sudo bin/use_new.sh` | Switch to new stack (foreground) |
| `sudo bin/use_new.sh --service` | Switch to new stack (systemd) |
| `sudo bin/use_old.sh` | Switch to old stack |
| `systemctl start/stop/restart/status cracked-bot` | Manage v2 service |
| `journalctl -u cracked-bot -f` | Follow v2 logs |