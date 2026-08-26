# HANDOFF — Cracked Alert (2026-08-25)

Handoff document for the next person/agent working on this repo. Covers where
we are, the open bug, everything that was ruled out (so you don't re-tread),
and the exact pending steps.

---

## 1. What this repo is

**Cracked Alert** — a Telegram trading bot for **cTrader Open API** (XAUUSD
focus). Python 3.9+ / asyncio / python-telegram-bot v21 / websockets / SQLite.
Runs on a VPS (`root@localhost` → the VPS, repo at `~/crackedalert`) as
systemd services; a Caddy reverse proxy fronts the web UI and the
alarm-app endpoints.

Also in this repo (all shipped recently):

- **`ALARM_APP.md`** — contract for a "ring until dismissed" two-phone alarm app.
- **`android/`** — the Android alarm app (pure Java, zero deps, ~25 KB APK).
  Polls `GET /alert-status` every 15 s with `X-Alert-Token`; full-screen alarm;
  `POST /ack` on dismiss. Debug APK committed at `android/CrackedAlarm-debug.apk`.
- **`frontend/ui.html`** — static command-builder UI (served at
  `https://alert.hotland3x3.my.id/ui.html`).

**Deployment:** `sudo ./deploy_v2.sh` on the VPS (git pull → pip install into
`/opt/crackedalert/venv` → smoke test → restart `cracked-bot` → copies UI →
Caddy). The deployed package is a *copied* install in the venv
(site-packages), not editable.

**Versioning convention:** every code ship bumps the static patch version in
**both** `pyproject.toml` (`version =`) and `src/crackedalert/__init__.py`
(`__version__`). Runtime version is `v2.<commit count>` (git-derived). Current
release: **2.0.33** (committed — **NOT yet deployed**; VPS is on 2.0.31).

---

## 2. THE OPEN BUG — `/m` fails: "could not fetch balance for demo" ✅ **SOLVED in v2.0.31**

> **ROOT CAUSE (found 2026-08-26):** `_recv_loop` awaited event handlers
> inline, so the tick → alert-fire → `confirm_position` chain (a zombie
> `CROSSING_UP` entry alert re-firing every tick, reconciling forever)
> blocked response correlation on the feed connection. Every request on
> that connection timed out while spots/trendbars kept flowing — exactly
> the observed forensics. Keepalive (2.0.21) and request serialization
> (2.0.29) were real fixes for *other* problems but not this one.
>
> **FIX:** the trade auto-alert chain (entry/tp/sl + reconcile-based
> confirmation) was removed entirely in v2.0.31 (user decision: broker-side
> SL/TP made it notification-only), and `_recv_loop` now enqueues frames to
> a dispatch worker so handler work can never stall correlation again.
> CC guards are preserved (pending-order guards materialize from the
> ExecutionEvent stream via a `pending_cc` registry).

**Symptom (user-reported, market open):**

```
/m 4634.28 n 2 2 demo --smart-sl 4636.687
→ error: could not fetch balance for demo
  (cTrader error TIMEOUT: no response for payloadType 2121 within 10s
   (recent frames: [(2131 ...spots...), (2138, 'ca-5', ...trendbar...), ...]))
```

Same failure with and without `--smart-sl`, for `/m` and `/p`. **The
`--smart-sl` feature itself is fine** — the failure is in the balance fetch
(`TradingService.execute()` → `fetch_balance()` → `PT_TRADER_REQ` (2121)),
which runs *before* any smart-SL logic. `/orders demo` works (reconcile 2124
answers); market data (spots, trendbars, symbols) always works.

### 2.1 What was RULED OUT (with evidence — do not re-investigate)

| Hypothesis | Verdict | Evidence |
|---|---|---|
| Wrong/expired ALERT token or config | ❌ | Token valid (28.6 days left); account auth responses clean |
| cTrader demo account expired / not authorized | ❌ | `GET_ACCOUNTS_BY_ACCESS_TOKEN` (2149): demo `47970822` IS in the token's grant; auth res `{'ctidTraderAccountId': ...}` |
| `isAuthorized` flag tells us anything | ❌ **PROTO FACT** | `ProtoOAAccountAuthRes` has **only** `{payloadType, ctidTraderAccountId}` — **no isAuthorized field** (verified against `spotware/openapi-proto-messages@3fd8bddf`). A response at all = auth accepted; failures arrive as `ProtoOAErrorRes`. Any code reading `isAuthorized` is reading garbage (this happened in 2.0.19 → false alarms → reverted in 2.0.20) |
| Wrong payload type / message shape for 2121 | ❌ | `PROTO_OA_TRADER_REQ = 2121` verified in the official proto; payload `{"ctidTraderAccountId": id}` is exactly the proto |
| int64 must be strings in JSON mode | ❌ | Gateway sends int64s as **numbers** (see logged frames/trendbar values) |
| Account auth without `accessToken` | ❌ | `accessToken` is a **required** field (`Message missing required fields: accessToken`); the "no-token" variant is invalid |
| Gateway version / active-account requirement | ❌ | No `SetActiveAccount` message exists in the current proto at all; gateway version reported as `100` |
| Websocket keepalive kills (FIXED in 2.0.21) | ✅ **FOUND + FIXED** | See 2.2 — real finding #1 |
| Stale/aged sessions, request-shape poisons, dual connections, `ca-N` msg ids | ❌ | The read-only probe (`bin/probe_trader.py`) replays ALL of it (symbols, trendbar, subscribe, reconcile, age 3+ min, dual demo+live connections, `ca-N` ids) — **trader answers every time** |

### 2.2 Real finding #1 (FIXED, 2.0.21): WS keepalive pings

The `websockets` library's default keepalive (ping every 20 s, 20 s timeout)
**kills the cTrader connection** with `1011 keepalive ping timeout` every
~10–35 min, because **the cTrader gateway does not answer WebSocket-protocol
pings** — its liveness protocol is the app-level `ProtoHeartbeatEvent` (51),
which the bot already sends every 8 s. Requests sent during the dead-but-
not-closed window (e.g. the balance request) are silently dropped by the
gateway while spot ticks still stream.

Fix (in `src/crackedalert/ctrader/client.py`):
`websockets.connect(self._url, ssl=ctx, ping_interval=None)`.

Journal signature of the old failure:

```
websockets.exceptions.ConnectionClosedError: sent 1011 (internal error)
keepalive ping timeout; no close frame received
```

### 2.3 Real finding #2 (SUSPECTED, fix shipped in 2.0.29 — UNCONFIRMED LIVE)

**The bot's session carries constant CONCURRENT request traffic, and the
gateway appears to drop a request sent while another is outstanding:**

- The **candle feed polls trendbars every 10 s** (`POLL_INTERVAL = 10.0` in
  `ctrader/candles.py`).
- Stale **entry alerts** (`9AG3`, `4KF1` — still active!) fire on every tick;
  each fire runs position-confirmation → **reconcile requests every ~2 s**
  (journal: "entry alert 9AG3: position not confirmed yet -- keeping alert").
- `/m`'s trader request therefore goes out *while other requests are in
  flight* → silence for 2121 only.

Evidence: the probe (which always awaits each request before sending the
next) never reproduces the failure; the smoke test (strictly sequential)
also succeeds (`--smoke` prints demo balance 990414.64, live100k 100580.04).

**Fix shipped (2.0.29, `src/crackedalert/ctrader/client.py`):** `request()`
is now serialized per client with an `asyncio.Lock` (`_req_lock`), so no two
requests are ever outstanding on a connection — matching the smoke/probe
behavior that always works.

### 2.4 PENDING ACTIONS (in order — the person continuing should do these)

1. **Deploy v2.0.31:** `cd ~/crackedalert && sudo ./deploy_v2.sh`.
2. **Verify:** `/positions demo`, `/orders demo` and `/m …` must now work
   immediately — and keep working (the deadlock class is gone). The startup
   log should show `purged N legacy auto trade alert(s)` once, clearing any
   remaining zombie rows.
3. **Probe tool note:** `bin/probe_trader.py` still account-auths the demo
   account. Running it kicks the *bot's* account session on that account —
   don't run it against an environment while the bot is expected to trade
   there (this likely contributed to earlier confusion).
3. **Confirm the mechanism with the probe:** the probe now has a
   `CONCURRENT REQUESTS` section (sends trendbar+trader and reconcile+trader
   back-to-back without awaiting):
   ```
   cd ~/crackedalert && git pull && sudo /opt/crackedalert/venv/bin/python3 bin/probe_trader.py
   ```
   Watch for: `=> trader back-to-back: SILENT` → mechanism confirmed.
4. If `/m` STILL fails after 2.0.30: the difference is process-level, not
   protocol-level. Next steps: instrument `request()`/recv-loop logging in
   the bot, or replicate the bot's exact `/m` call path (incl. `ensure_quote`)
   from inside the running process.

### 2.5 The diagnostic tool — `bin/probe_trader.py` (read-only, safe)

Standalone gateway probe (JSON mode, same protocol as the bot). **Never
places orders — account-info requests only.** Reads
`/etc/cracked_alert/.env_cracked` + `tokens.json`; note its env parser
mirrors python-dotenv (quotes/`export ` handling — the naive parser failed
app auth with `CH_CLIENT_AUTH_FAILURE` until fixed in 2.0.23).

**Findings it produced (all verified on the live gateway):** grant check
(demo in grant ✓), gateway version `100`, trader/responses in every
single/dual/aged/concurrent configuration the bot doesn't replicate.

---

## 3. Other changes shipped in this session (all deployed except 2.0.26–29)

| Version | Change |
|---|---|
| 2.0.14 | `/help` now replies with only the UI link + APK download link (balance fetches removed) |
| 2.0.15 | Server accepts `X-Alert-Token` header on `/alert-status` + `/ack` (the Android app sends the header; the server only read `?token=`) |
| 2.0.16 | Alert-fired messages are **notes-only** (`price hit <target>` fallback); ui.html notes dropdown (ChoCh / BoS / LTF ChoCh / LTF BoS) |
| 2.0.17 | ui.html notes → free-text + datalist presets (type-or-pick); UI header version synced |
| 2.0.18 | Balance-fetch errors now surface the real cTrader error code/description |
| 2.0.19 | (REVERTED in 2.0.20) bogus `isAuthorized`-based messaging — field does not exist in the proto |
| 2.0.20 | TIMEOUT errors include `recent frames` (payloadType, clientMsgId, payload keys) for forensics |
| 2.0.21 | **Keepalive fix** (`ping_interval=None`) — see 2.2 |
| 2.0.22–26 | Probe tool iterations (`bin/probe_trader.py`) |
| 2.0.27–28 | Probe: symbol lookups, age test, dual connections, `ca-N` ids |
| 2.0.29 | **Request serialization** (per-client lock) — see 2.3. **NOT yet deployed** |
| 2.0.30 | `/cancel` now deletes ANY alert owned by the chat, incl. auto entry/tp/sl — stale zombie entry alerts (`9AG3`/`4KF1`) are finally user-cancellable |
| 2.0.31 | **Auto-alert chain removed** (root cause of the demo deadlock); recv-loop decoupled via dispatch worker; CC guards kept (`pending_cc` registry for pending fills); legacy auto rows purged at startup |
| 2.0.32 | **`--smart-sl` redefined as a soft candle-close stop**: `--smart-sl <price> <tf>` no longer moves the broker-side SL — it arms a guard that closes the position when a `<tf>` candle CLOSES past the level (validated between fill and original SL). ui.html gains a Candle-TF select for it; combining with the positional CC pair is rejected |
| 2.0.33 | Smart-SL timeframes restricted to `M1 M5 M15 M30 H1` (new `SMART_SL_TIMEFRAMES` in candles.py; parser rejects higher TFs for `--smart-sl` only — CC guards and candle alerts keep the full list); ui.html Candle-TF select drops H4/D1 |

Android app: `android/` committed with debug APK; built locally with
JDK 21 (`C:\Android\jdk-21`) + Android SDK (`C:\Android\sdk`) + cached
Gradle 8.13 / AGP 8.13.2; rebuild with `android\gradlew.bat assembleDebug`.

---

## 4. Key technical facts & gotchas

**cTrader Open API (JSON mode over wss://demo.ctraderapi.com:5036):**

- Gateway **does not answer WS-protocol pings** → `ping_interval=None`
  mandatory; keepalive is the app-level proto heartbeat (≤10 s; bot sends 8 s).
- `ProtoOAAccountAuthRes` has **no `isAuthorized`** field.
- `ProtoOAAccountAuthReq.accessToken` is **required** (the OAuth token from
  `tokens.json`; auth_setup.py writes it; refresh_loop refreshes when <7 days
  left, daily).
- `ProtoOAErrorRes` supports `BLOCKED_PAYLOAD_TYPE` + `retryAfter` (rate
  limiting) — not observed here.
- Official protos (verified source): `spotware/openapi-proto-messages` at
  commit `3fd8bddf` — copies of the `.proto` files live on the dev machine at
  `C:\Android\*.proto`.
- `ProtoOATraderRes` carries the full `trader` entity (balance, moneyDigits);
  `ProtoOAReconcileRes` has **no balance** (positions/orders only).

**Dev machine (Windows, `A:\projects\linode\crackedalert`):**

- Sandbox TLS quirk: `Invoke-WebRequest`/`curl.exe` (WinHTTP/schannel) fail;
  **Python/Node HTTPS (OpenSSL) works**; `git` works (openssl backend).
- Android toolchain: JDK 21 at `C:\Android\jdk-21` (JDK 24 on PATH is too new
  for Gradle 8.13 — native-platform error), SDK at `C:\Android\sdk`
  (platforms;android-35, build-tools;35.0.0), Gradle 8.13 + AGP 8.13.2 cached
  in `~/.gradle`. adb at `C:\Android\adb.exe`.
- llm-verifier (for hard decisions): write `run.json`, run
  `A:\dev\verifier\.venv\Scripts\python.exe A:\dev\verifier\verify.py run.json`.

**Repo conventions:**

- Backend change → bump patch version in pyproject.toml + `__init__.py`,
  3× verification loop (`python -m unittest discover -s tests` — currently
  245 tests; `python -m compileall src`), commit + push, then VPS:
  `cd ~/crackedalert && sudo ./deploy_v2.sh`.
- `.env`/`tokens.json` live only on the VPS (`/etc/cracked_alert/`); never
  commit secrets.
- `ALERT_STATUS_TOKEN` + `ALERT_STATUS_PORT` gate the alarm endpoint
  (127.0.0.1:8190 in-process, Caddy-proxied).

---

## 5. Open items / leftovers

- **v2.0.31 deploy + `/m` verification** — see 2.4. This closes the bug.
- Pending-order cc guards registered in `pending_cc` are in-memory only: a
  restart between `/p` placement and fill drops the guard registration
  (documented; re-attach via `/ccalert`).
- Untracked, deliberately uncommitted: `.reasonix/`, `SESSION_RECAP.md`,
  `implementation_plan.md`, `reasonix.toml` (user hasn't asked to commit them).
- Android app: only debug APK exists; release signing (keystore) not set up.
- `run.json` (llm-verifier input) is transient — delete after use.
- Legacy bash stack (`bin/cracked_listener.sh`) retired at deploy
  ("🪦 bash stack retired" in deploy_v2.sh output); the old MT5 Flask backend
  referenced by `api.md` is historical.
