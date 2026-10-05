"""OAuth token storage and refresh.

The refresh token is single-use: every refresh returns a NEW pair, and the
old refresh token dies. Persistence is therefore atomic (tmp + os.replace)
and happens before anything else touches the new tokens.
"""

import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Callable, Optional

log = logging.getLogger("crackedalert.tokens")

TOKEN_URL = "https://openapi.ctrader.com/apps/token"
REFRESH_AHEAD_SECONDS = 7 * 86400      # refresh when <7 days left
CHECK_INTERVAL_SECONDS = 86400         # daily


class TokenError(Exception):
    pass


class TokenStore:
    def __init__(self, path: str, client_id: str, client_secret: str):
        self._path = path
        self._client_id = client_id
        self._client_secret = client_secret
        self._data = self._load()
        # In-flight refresh task: the live and demo clients share one
        # TokenStore and can hit their self-heal in the same second; the
        # refresh token is single-use, so concurrent callers must share
        # ONE rotation instead of racing two.
        self._refresh_task: Optional[asyncio.Future] = None

    def _load(self) -> dict:
        if not os.path.exists(self._path):
            raise TokenError(
                "%s not found -- run auth_setup.py first" % self._path)
        with open(self._path) as f:
            return json.load(f)

    @property
    def access_token(self) -> str:
        return self._data["accessToken"]

    def _seconds_left(self) -> Optional[float]:
        expires_at = self._data.get("expiresAt")
        return None if not expires_at else expires_at - time.time()

    def needs_refresh(self) -> bool:
        left = self._seconds_left()
        return left is not None and left < REFRESH_AHEAD_SECONDS

    def _save_atomic(self, data: dict) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self._path)

    def _refresh_blocking(self) -> None:
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self._data["refreshToken"],
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }).encode()
        req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=30) as resp:
            answer = json.loads(resp.read().decode())

        access = answer.get("accessToken") or answer.get("access_token")
        refresh = answer.get("refreshToken") or answer.get("refresh_token")
        expires = answer.get("expiresIn") or answer.get("expires_in")
        if not access or not refresh:
            raise TokenError("refresh rejected: %s" % json.dumps(answer))

        data = {
            "accessToken": access,
            "refreshToken": refresh,
            "obtainedAt": int(time.time()),
            "expiresAt": int(time.time()) + int(expires) if expires else None,
        }
        self._save_atomic(data)   # persist BEFORE adopting (single-use token)
        self._data = data
        log.info("access token refreshed, ~%s days validity",
                 int(expires) // 86400 if expires else "?")

    async def refresh(self) -> None:
        # One rotation at a time: concurrent callers (both clients'
        # self-heal firing together) await the same in-flight future, so
        # a single-use refresh token is never submitted twice.
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.get_event_loop().run_in_executor(
                None, self._refresh_blocking)
        await self._refresh_task

    async def exchange_code(self, code: str, redirect_uri: str) -> None:
        """Trade a one-time authorization code for a fresh token pair.

        The human path for a revoked grant: the operator approves access
        in a browser (the token-dead alert links the consent page) and
        pastes the redirect URL back into the chat; this completes the
        exchange, persists the new pair atomically and adopts it, so the
        reconnecting clients pick it up without a restart.
        """
        await asyncio.get_event_loop().run_in_executor(
            None, self._exchange_code_blocking, code, redirect_uri)

    def _exchange_code_blocking(self, code: str, redirect_uri: str) -> None:
        body = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }).encode()
        req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=30) as resp:
            answer = json.loads(resp.read().decode())

        access = answer.get("accessToken") or answer.get("access_token")
        refresh_tok = answer.get("refreshToken") or answer.get("refresh_token")
        expires = answer.get("expiresIn") or answer.get("expires_in")
        if not access or not refresh_tok:
            raise TokenError("code exchange rejected: %s" % json.dumps(answer))

        data = {
            "accessToken": access,
            "refreshToken": refresh_tok,
            "obtainedAt": int(time.time()),
            "expiresAt": int(time.time()) + int(expires) if expires else None,
        }
        self._save_atomic(data)   # persist BEFORE adopting
        self._data = data
        log.info("access token replaced from a fresh authorization code")

    async def refresh_loop(
            self,
            on_failure: Optional[Callable[[str], "asyncio.Future"]] = None
    ) -> None:
        """Daily check; refresh when close to expiry. Run as a task."""
        while True:
            try:
                if self.needs_refresh():
                    await self.refresh()
            except Exception as e:
                log.error("token refresh failed: %s", e)
                if on_failure is not None:
                    try:
                        await on_failure(str(e))
                    except Exception:
                        log.exception("refresh failure callback failed")
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)


# --------------------------------------------------------------------------
# dead-grant recovery: a revoked grant cannot be refreshed -- the refresh
# token dies with it. The only fix is a fresh human consent, which yields
# a one-time authorization code. These helpers carry that flow: build the
# consent URL for the alert, pull the code out of the pasted redirect,
# exchange it, and rate-limit the "grant is dead" pings.
# --------------------------------------------------------------------------

AUTHORIZE_ENDPOINT = "https://id.ctrader.com/my/settings/openapi/grantingaccess/"
_CODE_RE = re.compile(r"\bcode=([^&\s]+)")


def authorize_url(client_id: str, redirect_uri: str) -> str:
    """Consent-page URL for the operator to approve access again."""
    return "%s?%s" % (AUTHORIZE_ENDPOINT, urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "trading",
        "product": "web",
    }))


def extract_code(pasted: str) -> Optional[str]:
    """Pull the authorization code out of a pasted redirect URL.

    Forgiving about surrounding text ("sure: https://... thanks") and
    about which query position the code sits in. Returns None when the
    message carries no code at all.
    """
    if not pasted:
        return None
    m = _CODE_RE.search(pasted)
    return m.group(1) if m else None


def make_selfheal_refresh(
        store: "TokenStore",
        on_dead: Callable[[Exception], "asyncio.Future"],
        min_interval: float = 6 * 3600) -> Callable[[], "asyncio.Future"]:
    """Wrap TokenStore.refresh for the reconnect self-heal path.

    The reconnect loop calls the wrapped refresh after every
    REFRESH_AFTER_FAILURES failures. When the refresh itself keeps
    failing (a revoked grant -- only a fresh consent fixes that), ping
    on_dead at most once per min_interval so the operator learns within
    minutes without a reconnect storm spamming them. The refresh error
    always propagates; a failing on_dead must not mask it.
    """
    last = {"at": -min_interval}

    async def selfheal_refresh() -> None:
        try:
            await store.refresh()
        except Exception as e:
            now = time.monotonic()
            if now - last["at"] >= min_interval:
                last["at"] = now
                try:
                    await on_dead(e)
                except Exception:
                    log.exception("token-dead notify failed")
            raise

    return selfheal_refresh
