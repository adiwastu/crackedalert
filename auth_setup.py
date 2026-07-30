#!/usr/bin/env python3
"""
One-time OAuth setup for the Cracked Alert cTrader bot.

Walks through the cTrader Open API authorization flow:
  1. Prints the authorize URL -- open it, log in, approve.
  2. Your browser lands on the redirect URI with ?code=... -- paste that here.
  3. Exchanges the code for access + refresh tokens.
  4. Connects to cTrader and lists every trading account the token can see
     (copy the ids into CTRADER_ACCOUNTS in your .env).
  5. Writes tokens.json next to this script (move it to
     /etc/cracked_alert/tokens.json on the VPS).

Needs: pip install websockets
"""

import asyncio
import getpass
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request

try:
    import websockets
except ImportError:
    sys.exit("missing dependency -- run: pip install websockets")

AUTH_URL = "https://id.ctrader.com/my/settings/openapi/grantingaccess/"
TOKEN_URL = "https://openapi.ctrader.com/apps/token"
# Account list works from either environment host; demo is used here.
WS_HOST = "wss://demo.ctraderapi.com:5036"

DEFAULT_REDIRECT = "https://hotland3x3.my.id/sable/callback"
TOKENS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokens.json")

# ProtoOA payload types (JSON mode uses the same numbering as protobuf)
PT_APPLICATION_AUTH_REQ = 2100
PT_APPLICATION_AUTH_RES = 2101
PT_ERROR_RES = 2142
PT_GET_ACCOUNT_LIST_REQ = 2149
PT_GET_ACCOUNT_LIST_RES = 2150
PT_HEARTBEAT = 51


def ask(prompt, default=None):
    label = "%s [%s]: " % (prompt, default) if default else "%s: " % prompt
    val = input(label).strip()
    return val or (default or "")


def extract_code(pasted):
    """Accept either the bare code or the full redirected URL."""
    pasted = pasted.strip()
    if "code=" in pasted:
        query = urllib.parse.urlparse(pasted).query
        params = urllib.parse.parse_qs(query)
        if "code" in params:
            return params["code"][0]
    return pasted


def exchange_code(client_id, client_secret, code, redirect_uri):
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())

    # Spotware has used both camelCase and snake_case across doc versions.
    access = body.get("accessToken") or body.get("access_token")
    refresh = body.get("refreshToken") or body.get("refresh_token")
    expires = body.get("expiresIn") or body.get("expires_in")
    if not access:
        raise RuntimeError("token exchange failed: %s" % json.dumps(body))
    return access, refresh, int(expires or 0)


async def ws_request(ws, payload_type, payload, expect, timeout=15):
    """Send one frame and wait for the expected payloadType (or error)."""
    await ws.send(json.dumps({
        "clientMsgId": str(payload_type),
        "payloadType": payload_type,
        "payload": payload,
    }))
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("timed out waiting for payloadType %d" % expect)
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        pt = frame.get("payloadType")
        if pt == expect:
            return frame.get("payload", {})
        if pt == PT_ERROR_RES:
            p = frame.get("payload", {})
            raise RuntimeError("cTrader error %s: %s" % (
                p.get("errorCode"), p.get("description")))
        # Anything else (heartbeats, unsolicited events): keep waiting.


async def fetch_accounts(client_id, client_secret, access_token):
    ctx = ssl.create_default_context()
    async with websockets.connect(WS_HOST, ssl=ctx) as ws:
        await ws_request(ws, PT_APPLICATION_AUTH_REQ, {
            "clientId": client_id,
            "clientSecret": client_secret,
        }, PT_APPLICATION_AUTH_RES)
        payload = await ws_request(ws, PT_GET_ACCOUNT_LIST_REQ, {
            "accessToken": access_token,
        }, PT_GET_ACCOUNT_LIST_RES)
        return payload.get("ctidTraderAccount", [])


def main():
    print("=== Cracked Alert -- cTrader OAuth setup ===\n")
    client_id = ask("Client ID")
    client_secret = getpass.getpass("Client Secret (hidden): ").strip()
    redirect_uri = ask("Redirect URI (exactly as registered)", DEFAULT_REDIRECT)
    if not client_id or not client_secret:
        sys.exit("client id/secret are required.")

    authorize = "%s?%s" % (AUTH_URL, urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "trading",
        "product": "web",
    }))
    print("\n1. Open this URL in your browser and approve access:\n")
    print("   %s\n" % authorize)
    print("2. You will land on %s?code=..." % redirect_uri)
    print("   (the page will look broken/404 -- that is fine)\n")
    code = extract_code(input("3. Paste the full URL or just the code: "))
    if not code:
        sys.exit("no code provided.")

    print("\nExchanging code for tokens...")
    access, refresh, expires_in = exchange_code(
        client_id, client_secret, code, redirect_uri)
    print("OK -- access token obtained (expires in ~%d days)." %
          (expires_in // 86400 if expires_in else 0))

    tokens = {
        "accessToken": access,
        "refreshToken": refresh,
        "obtainedAt": int(time.time()),
        "expiresAt": int(time.time()) + expires_in if expires_in else None,
    }
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2)
    try:
        os.chmod(TOKENS_FILE, 0o600)
    except OSError:
        pass
    print("Tokens written to %s" % TOKENS_FILE)

    print("\nDiscovering trading accounts...")
    accounts = asyncio.run(fetch_accounts(client_id, client_secret, access))
    if not accounts:
        print("No accounts returned. If your app is still pending approval,")
        print("this may start working once Spotware approves it.")
        return

    print("\nAccounts visible to this token:\n")
    print("  %-22s %-12s %s" % ("ctidTraderAccountId", "login", "environment"))
    for acc in accounts:
        print("  %-22s %-12s %s" % (
            acc.get("ctidTraderAccountId"),
            acc.get("traderLogin", "?"),
            "live" if acc.get("isLive") else "demo",
        ))
    print("\nCopy these ids into CTRADER_ACCOUNTS in your .env, e.g.:")
    print('  CTRADER_ACCOUNTS={"5k":{"id":<id>,"env":"live"}, ...}')


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\naborted.")
    except Exception as e:
        sys.exit("\nFAILED: %s" % e)
