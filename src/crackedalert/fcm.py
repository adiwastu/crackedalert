"""Firebase Cloud Messaging push: wake the alarm app instantly.

The alarm app previously relied on polling GET /alert-status every 15s,
which Android fights: Doze cuts background network and OEM battery
managers freeze whole processes, so the phone went quiet until a
reboot. A high-priority FCM data message is the mechanism Android
reserves for this: it is delivered through Doze and wakes the app, which
then starts its full-screen ring exactly as if a poll had seen the
alert. Polling stays as the fallback path.

Wire format: a DATA-ONLY message (no `notification` block). A
notification block would make Android render the message itself while
the app is backgrounded and never hand it to the app, killing the
full-screen alarm -- the app must build its own alarm UI from `data`.

Auth is FCM HTTP v1: a service-account JSON (from the Firebase console)
is exchanged for an OAuth access token via a signed JWT. That needs one
real dependency (cryptography) for the RS256 signature; everything else
is stdlib urllib, like the rest of the bot.
"""

import asyncio
import base64
import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib import request as urlrequest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger("crackedalert.fcm")

OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
OAUTH_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"

# The app should stop ringing for a push it cannot resolve (the
# registration was revoked server-side) -- these are permanent.
_TOKEN_GONE_STATUS = (404, 410)


@dataclass(frozen=True)
class ServiceAccount:
    project_id: str
    client_email: str
    private_key: str          # PEM, as embedded in the JSON


def load_service_account(path: str) -> ServiceAccount:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    sa = ServiceAccount(
        project_id=str(raw.get("project_id", "")),
        client_email=str(raw.get("client_email", "")),
        private_key=str(raw.get("private_key", "")),
    )
    if not sa.project_id or not sa.client_email or not sa.private_key:
        raise ValueError(
            "FCM service account file is missing project_id, "
            "client_email or private_key")
    return sa


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(sa: ServiceAccount, now: Optional[float] = None) -> str:
    """A signed JWT for the OAuth token exchange (RS256)."""
    now = time.time() if now is None else now
    header = _b64url(json.dumps(
        {"alg": "RS256", "typ": "JWT"},
        separators=(",", ":")).encode())
    claims = _b64url(json.dumps({
        "iss": sa.client_email,
        "scope": OAUTH_SCOPE,
        "aud": OAUTH_TOKEN_URL,
        "iat": int(now),
        "exp": int(now) + 3600,
    }, separators=(",", ":")).encode())
    signing_input = ("%s.%s" % (header, claims)).encode("ascii")
    key = serialization.load_pem_private_key(
        sa.private_key.encode(), password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return "%s.%s.%s" % (header, claims, _b64url(signature))


class FCMTokenStore:
    """Registered device tokens in cracked.db (survives restarts)."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS fcm_tokens (
        token    TEXT PRIMARY KEY,
        added_at INTEGER NOT NULL
    );
    """

    def __init__(self, db_path: str):
        self._db = sqlite3.connect(db_path)
        self._db.executescript(self.SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def add(self, token: str) -> bool:
        """True when new, False when already registered."""
        cur = self._db.execute(
            "INSERT OR IGNORE INTO fcm_tokens VALUES (?, ?)",
            (token, int(time.time())))
        self._db.commit()
        return cur.rowcount > 0

    def remove(self, token: str) -> None:
        self._db.execute("DELETE FROM fcm_tokens WHERE token = ?",
                         (token,))
        self._db.commit()

    def all_tokens(self) -> List[str]:
        return [r[0] for r in
                self._db.execute("SELECT token FROM fcm_tokens")]


def build_message(device_token: str, title: str, body: str,
                  since: float, urgent: bool = False) -> dict:
    """Data-only, high-priority push. Data values must be strings.

    urgent=1 tells the app to ring through its own quiet window (used
    only by the token-dead alarm, the one message that must reach the
    operator even asleep).
    """
    data = {
        "title": title,
        "detail": body,
        "since": str(int(since)),
    }
    if urgent:
        data["urgent"] = "1"
    return {
        "message": {
            "token": device_token,
            "data": data,
            "android": {"priority": "HIGH"},
        }
    }


class FCMSendError(Exception):
    """A messages:send failure, carrying the HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__("FCM %s: %s" % (status, message))
        self.status = status


class FcmSender:
    """Pushes alerts to every registered phone; one OAuth token cached."""

    def __init__(self, sa: ServiceAccount, store: FCMTokenStore):
        self._sa = sa
        self._store = store
        self._access_token: str = ""
        self._token_expiry: float = 0.0

    def _post(self, url: str, data: bytes,
              headers: dict) -> Tuple[int, bytes]:
        req = urlrequest.Request(url, data=data, headers=headers,
                                 method="POST")
        try:
            with urlrequest.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urlrequest.HTTPError as e:
            return e.code, e.read()

    def _get_access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._token_expiry - 60:
            return self._access_token
        assertion = make_jwt(self._sa, now)
        # Concatenated, not %-formatted: the grant_type value is itself
        # percent-encoded and those % signs must survive verbatim.
        data = ("grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3A"
                "jwt-bearer&assertion=" + assertion).encode()
        status, body = self._post(
            OAUTH_TOKEN_URL, data,
            {"Content-Type": "application/x-www-form-urlencoded"})
        if status != 200:
            raise FCMSendError(status, "oauth token exchange failed: %s"
                               % body[:200].decode("utf-8", "replace"))
        out = json.loads(body)
        self._access_token = str(out["access_token"])
        self._token_expiry = now + int(out.get("expires_in", 3600))
        return self._access_token

    def _send_one(self, device_token: str, title: str, body: str,
                  since: float, urgent: bool = False) -> None:
        access = self._get_access_token()
        url = FCM_SEND_URL.format(project=self._sa.project_id)
        payload = json.dumps(
            build_message(device_token, title, body, since,
                          urgent)).encode()
        status, resp = self._post(url, payload, {
            "Content-Type": "application/json; charset=UTF-8",
            "Authorization": "Bearer %s" % access,
        })
        if status >= 400:
            raise FCMSendError(status, resp[:200].decode("utf-8", "replace"))

    async def push_all(self, title: str, body: str, since: float,
                       urgent: bool = False) -> None:
        """Fan out to every registered phone; never raises.

        Tokens the server no longer knows (app uninstalled, registration
        expired) are dropped from the store so they stop costing a
        request on every alert.
        """
        tokens = self._store.all_tokens()
        if not tokens:
            return
        sent = gone = 0
        for token in tokens:
            try:
                await asyncio.to_thread(self._send_one, token, title,
                                        body, since, urgent)
                sent += 1
            except FCMSendError as e:
                if e.status in _TOKEN_GONE_STATUS:
                    self._store.remove(token)
                    gone += 1
                    log.info("fcm: dropped unregistered device token")
                else:
                    log.warning("fcm: push failed (%s): %s", token[:12], e)
            except Exception as e:
                log.warning("fcm: push failed (%s): %s", token[:12], e)
        log.info("fcm: pushed %s/%s devices (%s stale removed)",
                 sent, len(tokens), gone)
