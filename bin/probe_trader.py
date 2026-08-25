#!/usr/bin/env python3
"""Read-only cTrader gateway probe: why does PT_TRADER_REQ (2121) go unanswered
on the bot's long-lived session while fresh sessions answer it?

Sequence per auth variant (WITH token is the one that matters):
  auth -> trader (control) -> trendbar poll -> trader -> subscribe spots ->
  trader -> reconcile (control)

The bot's session differs from the smoke test's fresh session by doing
trendbar polls and spot subscriptions; this finds which one (if any)
makes the gateway stop answering 2121. NEVER places orders.

Usage (on the VPS):
    sudo /opt/crackedalert/venv/bin/python3 bin/probe_trader.py

Reads /etc/cracked_alert/.env_cracked + /etc/cracked_alert/tokens.json.
"""

import asyncio
import json
import ssl
import sys
import time

ENV_FILE = "/etc/cracked_alert/.env_cracked"
TOKENS_FILE = "/etc/cracked_alert/tokens.json"
HOST = "wss://demo.ctraderapi.com:5036"

PT_APP_AUTH_REQ = 2100
PT_APP_AUTH_RES = 2101
PT_ACCT_AUTH_REQ = 2102
PT_ACCT_AUTH_RES = 2103
PT_VERSION_REQ = 2104
PT_VERSION_RES = 2105
PT_TRADER_REQ = 2121
PT_TRADER_RES = 2122
PT_RECONCILE_REQ = 2124
PT_RECONCILE_RES = 2125
PT_SUBSCRIBE_SPOTS_REQ = 2127
PT_SUBSCRIBE_SPOTS_RES = 2128
PT_GET_TRENDBARS_REQ = 2137
PT_GET_TRENDBARS_RES = 2138
PT_GET_ACCOUNTS_REQ = 2149
PT_GET_ACCOUNTS_RES = 2150
PT_ERROR_RES = 2142

NAME = {
    51: "HEARTBEAT", 2100: "APP_AUTH_REQ", 2101: "APP_AUTH_RES",
    2102: "ACCT_AUTH_REQ", 2103: "ACCT_AUTH_RES",
    2104: "VERSION_REQ", 2105: "VERSION_RES",
    2121: "TRADER_REQ", 2122: "TRADER_RES",
    2124: "RECONCILE_REQ", 2125: "RECONCILE_RES",
    2127: "SUBSCRIBE_REQ", 2128: "SUBSCRIBE_RES",
    2131: "SPOT_EVENT", 2137: "TRENDBARS_REQ", 2138: "TRENDBARS_RES",
    2142: "ERROR_RES",
    2149: "GET_ACCOUNTS_REQ", 2150: "GET_ACCOUNTS_RES",
}

# XAUUSD symbol id on the demo account (from the bot's own logs).
SYMBOL_ID = 41
# Payload types too noisy to print (streamed continuously).
QUIET = {51, 2131}


def load_env():
    """Parse KEY=VALUE lines the way python-dotenv does (the bot uses
    load_dotenv): strip quotes around values and 'export ' prefixes."""
    out = {}
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k] = v
    return out


def demo_account(env):
    try:
        accs = json.loads(env.get("CTRADER_ACCOUNTS", "{}"))
    except (ValueError, TypeError):
        return None, None
    for short, info in accs.items():
        if str(info.get("env", "")).lower() == "demo":
            try:
                return int(info["id"]), short
            except (KeyError, TypeError, ValueError):
                continue
    return None, None


def fmt(frame):
    pt = frame.get("payloadType")
    name = NAME.get(pt, "PT_%s" % pt)
    msg = frame.get("clientMsgId")
    payload = frame.get("payload", {}) or {}
    if pt == PT_ERROR_RES:
        return "%s msg=%s ERROR %s: %s" % (
            name, msg, payload.get("errorCode"),
            payload.get("description"))
    if pt == PT_VERSION_RES:
        return "%s msg=%s version=%s" % (
            name, msg, payload.get("version"))
    keys = sorted(payload.keys())
    return "%s msg=%s keys=%s" % (name, msg, keys[:8])


async def send(ws, msg_id, pt, payload):
    try:
        await ws.send(json.dumps({
            "clientMsgId": msg_id, "payloadType": pt, "payload": payload}))
    except Exception as e:
        print("  send failed (connection closed?): %s" % e)


async def _heartbeat(ws):
    """ProtoOA heartbeat every 8s so the gateway keeps the session alive."""
    while True:
        await asyncio.sleep(8)
        try:
            await send(ws, None, 51, {})
        except Exception:
            return


async def recv_until(ws, deadline):
    try:
        return json.loads(await asyncio.wait_for(
            ws.recv(), max(0.1, deadline - time.monotonic())))
    except Exception:
        return None    # timeout or connection closed


async def await_msg(ws, msg_id, seconds, label, quiet=False):
    """Collect frames until the correlated response for msg_id arrives
    (or a correlated error), printing everything meanwhile."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        frame = await recv_until(ws, end)
        if frame is None:
            print("  %s: NO RESPONSE within %.0fs (silence)"
                  % (label, seconds))
            return None
        if not quiet and frame.get("payloadType") not in QUIET:
            print("  %s: %s" % (label, fmt(frame)))
        if frame.get("clientMsgId") == msg_id:
            if frame.get("payloadType") == PT_ERROR_RES:
                print("  %s: gateway ANSWERED with an error" % label)
            return frame
    print("  %s: NO RESPONSE within %.0fs (silence)" % (label, seconds))
    return None


async def probe_trader(ws, account_id, seq, seconds=8):
    """One TRADER_REQ step; returns the correlated frame or None."""
    msg_id = "p-t%d" % seq
    await send(ws, msg_id, PT_TRADER_REQ,
               {"ctidTraderAccountId": account_id})
    frame = await await_msg(ws, msg_id, seconds,
                            "trader #%d" % seq, quiet=True)
    print("  => trader #%d: %s" % (
        seq, "ANSWERED (%s)" % NAME.get(
            frame.get("payloadType"), frame.get("payloadType"))
        if frame else "SILENT"))
    return frame


async def run_variant(env, token, account_id, with_token):
    ctx = ssl.create_default_context()
    import websockets
    async with websockets.connect(HOST, ssl=ctx, ping_interval=None) as ws:
        label = "ACCOUNT AUTH WITH accessToken" if with_token \
            else "ACCOUNT AUTH without accessToken"
        print("\n=== %s ===" % label)

        await send(ws, "p-app", PT_APP_AUTH_REQ, {
            "clientId": env["CTRADER_CLIENT_ID"],
            "clientSecret": env["CTRADER_CLIENT_SECRET"],
        })
        heartbeat = asyncio.get_running_loop().create_task(
            _heartbeat(ws))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            frame = await recv_until(ws, deadline)
            if frame is None:
                print("  app auth: no response")
                return
            line = fmt(frame)
            print("  app-auth:", line)
            if frame.get("payloadType") == PT_APP_AUTH_RES:
                break
            if frame.get("payloadType") == PT_ERROR_RES:
                return

        await send(ws, "p-ver", PT_VERSION_REQ, {})

        payload = {"ctidTraderAccountId": account_id}
        if with_token:
            payload["accessToken"] = token
        await send(ws, "p-auth", PT_ACCT_AUTH_REQ, payload)

        # 1) AWAIT the account-auth outcome before any account-state request.
        auth_frame = await await_msg(ws, "p-auth", 10, "acct-auth")
        if auth_frame is None:
            return

        # 2) Account grant check: is our demo account visible to the token?
        await send(ws, "p-accs", PT_GET_ACCOUNTS_REQ,
                   {"accessToken": token})
        end = time.monotonic() + 10
        accounts = None
        while time.monotonic() < end:
            frame = await recv_until(ws, end)
            if frame is None:
                break
            if frame.get("clientMsgId") == "p-accs":
                accounts = frame.get("payload", {}).get(
                    "ctidTraderAccount", [])
                break
        if accounts is not None:
            found = [a for a in accounts
                     if int(a.get("ctidTraderAccountId", 0)) == account_id]
            print("  GRANT: token sees %d account(s); demo %d in grant: %s"
                  % (len(accounts), account_id, bool(found)))
        else:
            print("  GRANT: no account-list response (silence)")

        # 3) Poison hunt (with-token variant only): which session request
        #    makes the gateway stop answering 2121?
        if with_token:
            await probe_trader(ws, account_id, 1)      # control

            await send(ws, "p-tb", PT_GET_TRENDBARS_REQ, {
                "ctidTraderAccountId": account_id,
                "symbolId": SYMBOL_ID, "period": "M15",
                "toTimestamp": int(time.time() * 1000), "count": 100,
            })
            await await_msg(ws, "p-tb", 8, "trendbar", quiet=True)
            print("  => trendbar poll: done")
            await probe_trader(ws, account_id, 2)      # after trendbar

            await send(ws, "p-sub", PT_SUBSCRIBE_SPOTS_REQ, {
                "ctidTraderAccountId": account_id,
                "symbolId": [SYMBOL_ID],
            })
            await await_msg(ws, "p-sub", 8, "subscribe", quiet=True)
            print("  => spot subscription: done")
            await probe_trader(ws, account_id, 3)      # after subscribe

            await send(ws, "p-recon", PT_RECONCILE_REQ,
                       {"ctidTraderAccountId": account_id})
            r = await await_msg(ws, "p-recon", 8, "reconcile", quiet=True)
            print("  => reconcile control: %s"
                  % ("ANSWERED" if r else "SILENT"))
        else:
            await send(ws, "p-trader", PT_TRADER_REQ,
                       {"ctidTraderAccountId": account_id})
            await await_msg(ws, "p-trader", 8, "trader", quiet=True)


def main():
    env = load_env()
    token = ""
    try:
        token = json.load(open(TOKENS_FILE)).get("accessToken", "")
    except (OSError, ValueError):
        print("warning: cannot read %s" % TOKENS_FILE)
    account_id, shortcode = demo_account(env)
    if account_id is None:
        print("no demo account found in CTRADER_ACCOUNTS")
        return 1
    print("probe: demo account %s (id %d), token present: %s"
          % (shortcode, account_id, bool(token)))
    asyncio.run(run_variant(env, token, account_id, True))
    asyncio.run(run_variant(env, token, account_id, False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
