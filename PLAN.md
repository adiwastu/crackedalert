# Cracked Alert — Python Rewrite Plan (MT5 → cTrader Open API)

Status: **planning approved, not yet implemented**
Target: single Python service on the existing VPS, replacing both bash daemons and the MT5 Flask middleware entirely.

---

## 1. Goal

Replace this:

```
┌─ VPS ──────────────────────┐      ┌─ elsewhere ─────────┐
│ cracked_listener.sh (bash) │─curl→│ MT5 Flask API ×4    │→ MT5
│ cracked_checker.sh  (bash) │─curl→│ (hotland3x3.my.id)  │
│ cracked_alerts.tsv         │      └─────────────────────┘
└────────────────────────────┘
```

With this:

```
┌─ VPS ─────────────────────────────────────────────┐
│ crackedalert (one Python service, asyncio)        │
│  ├─ Telegram long-polling (commands)              │──→ api.telegram.org
│  ├─ cTrader WS connection(s) (prices + trading)   │──→ live.ctraderapi.com:5036
│  ├─ Alert engine (real-time, tick-driven)         │──→ demo.ctraderapi.com:5036
│  └─ SQLite (alerts + tokens)                      │
└───────────────────────────────────────────────────┘
```

No middleware. No REST wrapper. The bot talks to cTrader natively.

---

## 2. Key decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D1 | cTrader transport | **JSON over WebSocket, port 5036**, pure asyncio via `websockets` | Same message schema as protobuf but human-readable; avoids Twisted (official `OpenApiPy` SDK is Twisted-based, which clashes with python-telegram-bot's asyncio). One event loop for everything. Fallback if JSON mode misbehaves: `OpenApiPy` on Twisted's asyncio reactor. |
| D2 | Process model | **One systemd service** | Listener + checker collapse into one process with asyncio tasks. Replaces 2 units with 1. |
| D3 | Telegram library | **python-telegram-bot v21+** | Mature, async, handles long-poll/offset/retries (kills the `.tg_offset` file logic). |
| D4 | Storage | **SQLite** (stdlib `sqlite3`) at `/etc/cracked_alert/cracked.db` | Atomic, no more `sed -i` on a TSV. Writes are tiny/rare → stdlib sync driver is fine, no aiosqlite needed. One-shot import of existing TSV on first run. |
| D5 | Alert checking | **Tick-driven, not polled** | We already hold spot subscriptions for trading; the alert engine just consumes the same live tick stream. Fires on real bid/ask instead of stale M1 close (fixes the checker's version of the `/m` price bug). |
| D6 | Command auth | **`ALLOWED_CHAT_IDS` allowlist** — new | Current bash accepts trade commands from *any* chat that finds the bot. Real-money hole. Unknown chats get silently ignored (logged). |
| D7 | Command syntax & replies | **Identical to today** | `/m`, `/p`, `/alert`, `/list`, `/cancel`, `/help` — same argument order, same reply text formats. Zero retraining. |
| D8 | Price feed for alerts | One designated account's feed (config: `PRICE_FEED_ACCOUNT`, default `demo`) | Alerts are account-agnostic; any feed works. Demo feed keeps alert traffic off live connections. |
| D9 | Lot rounding | **Floor to volume step** (bash used printf round-half) | Never exceed the stated risk %. Deliberate small behavior change — veto if unwanted. |

---

## 3. cTrader protocol facts the code must respect

Auth chain (per connection, in order):
1. TCP/WSS connect → `ProtoOAApplicationAuthReq` (client_id + secret) — must be first message
2. `ProtoOAAccountAuthReq` per account (ctidTraderAccountId + OAuth access token, `trade` scope)
3. Then symbols list, subscriptions, orders.

Hard rules:
- **Heartbeat**: send `ProtoHeartbeatEvent` every ≤10s or get disconnected. We send every 8s.
- **Live/demo are separate hosts** (`live.ctraderapi.com` / `demo.ctraderapi.com`); one connection cannot mix them → the connection manager opens one WS per environment actually in use.
- **Rate limits**: 50 req/s non-historical, 5 req/s historical. Our usage is orders of magnitude below.
- **`symbolId` is per-account.** XAUUSD's id on the 5k account ≠ on raven. Cache `{account → {symbol_name → symbolId, digits, lotSize, stepVolume, minVolume}}` at startup / re-auth.
- **OAuth tokens**: access token ~30-day expiry; refresh token is **single-use** — every refresh returns a new pair which must be persisted atomically (write-temp-then-rename) before use. Losing a rotated refresh token = manual re-auth.

### Scaling gotchas (mixed! biggest foot-gun in the protocol)

| Field | Encoding | Convert |
|---|---|---|
| `ProtoOASpotEvent.bid/ask` | uint64, price × 100 000 | ÷ 1e5, round to symbol `digits` |
| `ProtoOATrader.balance` | int64, scaled by `moneyDigits` | ÷ 10^moneyDigits |
| `ProtoOANewOrderReq.stopLoss/takeProfit/limitPrice/stopPrice` | **plain doubles, absolute prices** | no scaling |
| `ProtoOANewOrderReq.volume`, `ProtoOASymbol.lotSize/minVolume/stepVolume` | int64, in 0.01 units | `volume = lots × lotSize`; verify empirically on demo (Phase 4 gate) |

XAUUSD sanity check: lotSize is expected ≈ 10 000 (= 100 oz × 100). 0.01 lots → volume 100. **Must be confirmed on demo before any live order.**

Order result flow: `ProtoOANewOrderReq` → async `ProtoOAExecutionEvent` (ORDER_ACCEPTED / ORDER_FILLED …) or `ProtoOAOrderErrorEvent`. Correlate by account + wait-with-timeout after send; extract order id for the Telegram "ticket" reply.

---

## 4. Project structure

```
crackedalert/
├── PLAN.md                    ← this file
├── pyproject.toml             # deps: python-telegram-bot, websockets, python-dotenv
├── .env.example               # new shape, see §7
├── auth_setup.py              # one-time OAuth CLI (see §7)
├── src/crackedalert/
│   ├── main.py                # wiring: config → connections → bot → run
│   ├── config.py              # env parsing, account map dataclasses
│   ├── ctrader/
│   │   ├── client.py          # WS connect, auth chain, heartbeat, reconnect+resubscribe,
│   │   │                      #   request/response correlation (clientMsgId), timeouts
│   │   ├── market.py          # symbol cache, spot subscriptions, latest-price store
│   │   ├── trading.py         # balance fetch, market/pending order placement, volume calc
│   │   └── tokens.py          # token store + refresh loop (atomic rotation)
│   ├── bot/
│   │   ├── handlers.py        # /m /p /alert /list /cancel /help + chat-id gate
│   │   └── formatting.py      # reply templates (kept byte-compatible with bash)
│   ├── risk.py                # pure math: direction/widen/RR/lots — unit-tested
│   └── alerts.py              # SQLite store + tick-driven crossing engine + TSV import
├── systemd/cracked-bot.service    # replaces both old units
└── deploy.sh                      # updated (venv install, single service)
```

Estimated size: ~1 000–1 400 lines Python. `risk.py` is pure functions with zero I/O so math parity is unit-testable.

---

## 5. Functional spec (port-exact unless flagged)

### `/m [sl] [widen y/n] [rr] [risk%] [acct]` and `/p [entry] [sl] [widen] [rr] [risk%] [acct]`

1. Gate on `ALLOWED_CHAT_IDS` *(new)*.
2. Resolve account → environment/connection; unknown → `error: account 'X' not found.`
3. Fresh balance via `ProtoOATraderReq`.
4. Live bid/ask from the spot price store (subscription already running; stale >10s → refuse with error rather than trade on a dead feed *(new safety)*).
5. Math (identical to bash, in `risk.py`):
   - direction: `sl < entry_ref → BUY else SELL`
   - `/m` entry_ref: **ask** for BUY, **bid** for SELL (direction pre-inferred vs mid)
   - widen `y`: SL ∓ 1.00 (“tambah 10 pips” label)
   - `dist = |entry − sl|`; `tp = entry ± dist×rr`
   - `risk_usd = balance × risk%/100`; `lots = risk_usd / (dist × 100)` — the 100 becomes `lotSize/100` from symbol info (same number for XAUUSD, no longer hardcoded)
   - clamp to minVolume, **floor** to stepVolume (D9)
6. `/p` placement price: BUY `entry + spread`, SELL `entry − spread` (spread = ask − bid); math stays on raw entry (current behavior, kept).
7. `/p` order type inference *(moves here from the old Flask "smart order")*, using the **adjusted** price:
   - BUY: adj < ask → `LIMIT` (limitPrice), adj > ask → `STOP` (stopPrice), equal → LIMIT
   - SELL: adj > bid → `LIMIT`, adj < bid → `STOP`, equal → LIMIT
8. Send `ProtoOANewOrderReq`, await execution/error event (10s timeout), reply with the same success/failure text formats as today (order id as “ticket”).

### `/alert`, `/list`, `/cancel`, `/help`
- Same syntax, same replies, same 4-char IDs. Backed by SQLite instead of TSV.
- Engine: on each spot event, fire alerts where `CROSSING_UP ∧ price ≥ target` or `CROSSING_DOWN ∧ price ≤ target` (bid/ask midpoint = today's "close" analog; simple and consistent). Delete after firing. Alert set/fire messages unchanged.
- First run: import rows from `/etc/cracked_alert/cracked_alerts.tsv` if present, then rename it `.imported`.

---

## 6. Reliability

- **Reconnect**: exponential backoff (1s→60s cap) per connection; on reconnect re-run full auth chain + re-subscribe spots; price store marked stale meanwhile (blocks trading, not alert storage).
- **Token refresh loop**: daily check; refresh when <7 days left; atomic persist; on hard failure → Telegram message to first allowed chat ("re-run auth_setup.py").
- **Request timeouts** everywhere (10s default); every failure path replies with an error message rather than silence.
- **Logging**: stdlib logging → journald via systemd (same `journalctl -u cracked-bot -f` workflow).

---

## 7. Config & one-time setup

`.env` (new shape, lives at `/etc/cracked_alert/.env_cracked`):

```
TELEGRAM_BOT_TOKEN=...
ALLOWED_CHAT_IDS=123456789            # comma-separated
CTRADER_CLIENT_ID=...                 # from cTrader developer portal
CTRADER_CLIENT_SECRET=...
CTRADER_ACCOUNTS={"5k":{"id":11111,"env":"live"},"10k":{"id":22222,"env":"live"},"raven":{"id":33333,"env":"live"},"demo":{"id":44444,"env":"demo"}}
PRICE_FEED_ACCOUNT=demo
```

Tokens live separately at `/etc/cracked_alert/tokens.json` (0600, machine-written).

`auth_setup.py` (one-time, interactive):
1. Prints the `connect.spotware.com/apps/auth` URL (scope=trading) → user authorizes in browser
2. User pastes the redirect `code` → script exchanges it at `openapi.ctrader.com/apps/token`
3. Script calls `ProtoOAGetAccountListByAccessTokenReq` and **prints every ctidTraderAccountId + login it finds** → user copies ids into `CTRADER_ACCOUNTS`
4. Writes `tokens.json`

User prerequisites (Phase 0, manual):
- cTrader ID; create an application in the Open API portal (get client id/secret)
- Know which of 5k/10k/raven/demo exist as cTrader accounts and whether each is live or demo

---

## 8. Migration phases

| Phase | Deliverable | Gate to proceed |
|---|---|---|
| **0. Prereqs** (user) | cTrader app credentials; account inventory; run `auth_setup.py` | tokens.json exists, account ids known |
| **1. Skeleton + connection** | config, WS client, auth chain, heartbeat, reconnect; logs demo balance correctly | balance matches cTrader app; survives forced disconnect |
| **2. Market data + alerts** | symbol cache, spot subscribe, price store; `/alert /list /cancel /help`; SQLite + TSV import; alert engine | alert set on demo feed fires at the right price |
| **3. Trading** | `/m` and `/p` full flow, **hard-coded demo-only guard** | orders visible in cTrader demo with correct SL/TP/lots |
| **4. Parity verification** | unit tests on `risk.py` (golden cases = bash examples from `/help`); volume-scaling empirical check; side-by-side math vs current bash on same inputs | numbers identical (except D9 flooring); volume conversion confirmed |
| **5. Cutover** | remove demo guard; new systemd unit + deploy.sh; stop/disable old two units; decommission MT5 Flask APIs | a real `/m` on smallest live account lands correctly |

Rollback at any phase: old bash units + MT5 APIs stay untouched and running until Phase 5 sign-off. Note: XAUUSD is closed weekends — schedule Phases 2–5 verification on a weekday market session.

---

## 9. Risks / open items

1. **Volume scaling** (0.01-unit encoding vs lotSize) — highest-risk conversion; gated by Phase 4 empirical check before any live order.
2. **JSON mode (5036) docs are thinner than protobuf's** — mitigations: same message/field names as the published .proto files; fallback D1 to OpenApiPy.
3. **Refresh-token rotation** — single-use token lost mid-rotation forces manual re-auth; mitigated by atomic write, and worst case is re-running `auth_setup.py`.
4. **Account inventory unknown** — do all four accounts exist on cTrader? Resolved in Phase 0.
5. **Execution-event correlation** in a multi-account setup — mitigation: per-account serialized order placement (one in-flight order per account; commands are human-typed, so contention is nil).
