#!/usr/bin/env python3
"""Read-only cTrader gateway probe: why does PT_TRADER_REQ (2121) go unanswered?

Connects to the demo gateway in JSON mode and, for each account-auth variant
(with and without the access token), sends PT_TRADER_REQ + PT_RECONCILE_REQ
and prints EVERY frame received for ~12s (responses, errors, silence). Also
queries the gateway version.

NEVER places orders -- account-info requests only.

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
PT_ERROR_RES = 2142

NAME = {
    51: "HEARTBEAT", 2100: "APP_AUTH_REQ", 2101: "APP_AUTH_RES",
    2102: "ACCT_AUTH_REQ", 2103: "ACCT_AUTH_RES",
    2104: "VERSION_REQ", 2105: "VERSION_RES",
    2121: "TRADER_REQ", 2122: "TRADER_RES",
    2124: "RECONCILE_REQ", 2125: "RECONCILE_RES",
    2131: "SPOT_EVENT", 2142: "ERROR_RES",
}


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
    keys = sorted(payload.keys())
    return "%s msg=%s keys=%s" % (name, msg, keys[:8])


async def send(ws, msg_id, pt, payload):
    await ws.send(json.dumps({
        "clientMsgId": msg_id, "payloadType": pt, "payload": payload}))


async def _heartbeat(ws):
    """ProtoOA heartbeat every 8s so the gateway keeps the session alive
    during the 12s probe window."""
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
    except asyncio.TimeoutError:
        return None


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
        await send(ws, "p-trader", PT_TRADER_REQ,
                   {"ctidTraderAccountId": account_id})
        await send(ws, "p-recon", PT_RECONCILE_REQ,
                   {"ctidTraderAccountId": account_id})

        end = time.monotonic() + 12
        trader_seen = recon_seen = auth_seen = False
        while time.monotonic() < end:
            frame = await recv_until(ws, end)
            if frame is None:
                break
            line = fmt(frame)
            print("  ", line)
            msg = frame.get("clientMsgId")
            if msg == "p-trader":
                trader_seen = True
            if msg == "p-recon":
                recon_seen = True
            if msg == "p-auth":
                auth_seen = True
            if frame.get("payloadType") == PT_ERROR_RES and msg in (
                    "p-trader", "p-recon"):
                print("    ^ the gateway ANSWERED with an error for %s"
                      % msg)
        print("  RESULT: trader responded=%s reconcile responded=%s "
              "auth responded=%s"
              % (trader_seen, recon_seen, auth_seen))


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
