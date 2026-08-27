# CrackedAlert

Telegram bot for cTrader. Watches live forex prices and fires alerts when a price level is hit. Can also place, manage, and close trades directly from Telegram commands. Runs on Linux as a systemd service.

---

## Commands

| Command | Action |
|---|---|
| `/alert 2450.00` | Alert when XAUUSD reaches 2450 |
| `/alert 2450 EURUSD note` | Alert for a different symbol with a note |
| `/m 2440 y 2.0 0.5 main` | Market order: SL=2440, widen=yes, RR=2, risk=0.5%, account=main |
| `/p 2460 2440 y 2.0 0.5 main` | Pending order at 2460 |
| `/ccalert M15 2440 below` | Alert when a 15-minute candle closes below 2440 |
| `/orders main` | List open orders on account "main" |
| `/close 123456 main` | Close position 123456 |
| `/be main` | Move all stops to breakeven |

---

## Stack

| | |
|---|---|
| Python 3.9+ | |
| python-telegram-bot v21 | Native async, stable PTB API |
| WebSocket (port 5036, JSON mode) | cTrader Open API; JSON avoids a Protobuf dependency |
| SQLite | Stores alerts; no server needed for a personal tool |
| systemd | Process supervisor; handles restart on crash |
| Caddy | Serves the optional HTML status page |

---

## Architecture

```
Telegram -> python-telegram-bot -> Handlers
                                       |
                    +------------------+------------------+
                    |                  |                  |
               AlertStore        FeedService        TradingService
               (SQLite)         (cTrader WS)       (cTrader WS)
                                       |
                            CTraderClient (one per env)
                               live / demo
```

`CTraderClient` owns the WebSocket connection to cTrader. It handles heartbeats (required every 10s), request/response correlation via `clientMsgId`, and reconnection with exponential backoff. One instance runs per environment; live and demo run simultaneously.

`FeedService` subscribes to spot price events and checks alert thresholds on every tick. `TradingService` places and manages orders. `AlertStore` persists alerts to SQLite.

---

## Configuration

All secrets go in `/etc/cracked_alert/.env_cracked`. Nothing is hardcoded.

```env
TELEGRAM_BOT_TOKEN=
ALLOWED_CHAT_IDS=123456789,987654321
CTRADER_CLIENT_ID=
CTRADER_CLIENT_SECRET=
PRICE_FEED_ACCOUNT=demo
TRADE_SYMBOL=XAUUSD

# JSON: shortcode -> {env, id}
CTRADER_ACCOUNTS={"main":{"env":"live","id":12345678},"demo":{"env":"demo","id":87654321}}
```

`ALLOWED_CHAT_IDS` controls which chats can use gated commands. Additional chats can subscribe dynamically with `/subscribe`.

For the alarm-app endpoint (ring until dismissed), see [`ALARM_APP.md`](ALARM_APP.md) and set `ALERT_STATUS_TOKEN` / `ALERT_STATUS_PORT`.

---

## Local Setup

```bash
pip install -e .
mkdir -p data
python auth_setup.py
crackedalert run
```

Create `.env` in the project root with the vars above. Set `CRACKED_DATA_DIR=./data`.

---

## Deploy

```bash
sudo ./deploy_v2.sh
```

Installs into `/opt/crackedalert`, runs a connection smoke test, restarts the `cracked-bot` systemd service, and serves the UI via Caddy. Check logs with `journalctl -u cracked-bot -f`.

Set `YOUR_DOMAIN` in `deploy/caddy-ui.caddyfile` to your server's domain before deploying.

---

## Hard Parts

**Request/response correlation over a shared WebSocket.** The cTrader API multiplexes everything on one connection. The client stores a `Future` per `clientMsgId` and resolves it when the matching frame arrives. When the connection drops, all pending futures are immediately rejected so callers do not hang until timeout.

**Reconnection ordering.** After a disconnect, the `on_connected` callback re-authenticates accounts and re-subscribes to feeds. The recv loop must already be running when `on_connected` fires, because that callback makes requests whose responses only the recv loop can deliver. Starting it after `on_connected` deadlocks every request until timeout.

**Candle-close vs. tick alerts.** Tick alerts fire on every price update. Candle-close alerts only fire when a timeframe period ends with the close above or below the threshold. A price crossing mid-candle is not a candle close.
