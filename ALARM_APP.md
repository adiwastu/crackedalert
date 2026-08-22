# Alarm App API — "ring until dismissed" mobile app contract

This documents the HTTPS endpoint the bot exposes so a small mobile app (on
your phone and a consenting friend's) can ring until someone dismisses the
alert. The bot does **not** do the ringing — it only serves the "an alert is
active right now" state; the app decides to ring and keeps ringing until the
person taps dismiss and the app calls `/ack`.

## Enabling

The endpoint is off by default. On the VPS, set in
`/etc/cracked_alert/.env_cracked`:

```
ALERT_STATUS_TOKEN="<long-random-secret>"
ALERT_STATUS_PORT="8190"
```

Then restart the service. Empty/missing `ALERT_STATUS_TOKEN` disables the
endpoint entirely (returns nothing).

Externally, Caddy (`deploy/caddy-ui.caddyfile`) reverse-proxies these paths to
the in-process listener:

```
https://alert.hotland3x3.my.id/alert-status
https://alert.hotland3x3.my.id/ack
```

## Auth

Every request must carry the token as `?token=<ALERT_STATUS_TOKEN>` or the
`X-Alert-Token` header. A missing or wrong token returns `401`.

## Endpoints

### `GET /alert-status`

No alert firing:

```
200
{
  "active": false
}
```

An alert is firing:

```
200
{
  "active": true,
  "since": 1750000000,      // unix seconds the alert went live
  "detail": "auto SL hit for trade 42"
}
```

Bad token:

```
401
{"error": "unauthorized"}
```

### `POST /ack`

Clears the active flag (idempotent; auth required). The app should call this
when the user dismisses the alarm so the state goes back to `inactive` (and
the other phone stops ringing on its next poll).

```
200
{"ok": true}
```

## Suggested client behavior (the two phones)

1. Every ~10–20 s (in the background, with a system-alarm-approved battery
   whitelist) do `GET /alert-status?token=...`.
2. On `active:true`, fire a **full-screen looping alarm** (sound + strong
   vibration) that **does not auto-stop**.
3. Keep ringing until the user taps "dismiss".
4. On dismiss, `POST /ack?token=...`, stop the alarm. The next poll of the
   other phone then sees `active:false` and stops too.
5. If a poll fails (network), keep the previous decision: if it was ringing,
   keep ringing until an `/ack` succeeds — don't silently drop an alert on a
   transient network error.

## Notes

- The endpoint reveals only `active` / `since` / `detail` — never cTrader
  credentials, balances, tokens, or chat ids. Treat the token as a secret.
- `detail` is plain text (Telegram HTML is not exposed here).
- The alert is a **single shared flag**: any firing alert (manual, price,
  candle, TP/SL, entry) marks it active; `/ack` clears it. There is no
  per-alert acknowledgement.
- This is stdlib-only (`asyncio`), loopback-bound (`127.0.0.1`); only Caddy
  reaches it.
