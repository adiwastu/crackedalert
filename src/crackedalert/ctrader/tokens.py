"""OAuth token storage and refresh.

The refresh token is single-use: every refresh returns a NEW pair, and the
old refresh token dies. Persistence is therefore atomic (tmp + os.replace)
and happens before anything else touches the new tokens.
"""

import asyncio
import json
import logging
import os
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
        await asyncio.get_event_loop().run_in_executor(
            None, self._refresh_blocking)

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
