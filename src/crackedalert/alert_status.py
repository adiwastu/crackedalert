"""Tiny in-process HTTP endpoint exposing the bot's "active alert" state.

A small mobile alarm app on the user's phone polls ``GET /alert-status`` to
decide whether to ring until dismissed. This module is deliberately
stdlib-only (no new dependency): an asyncio server that answers two routes
inside the bot's existing event loop.

Routes (all require the ``ALERT_STATUS_TOKEN`` as ``?token=`` or the
``X-Alert-Token`` header):

    GET  /alert-status
        -> 200 {"active": false}                       (no alert running)
        -> 200 {"active": true, "since": <unix_ts>,
                "detail": "<escaped alert text>"}      (an alert is live)
        -> 401 {"error": "unauthorized"}               (bad/missing token)

    POST /ack
        -> 200 {"ok": true}                            (clears active state)
        -> 401 {"error": "unauthorized"}

The endpoint only ever reveals the alert's text and timestamp -- never
cTrader credentials, balances, tokens, or chat ids.
"""

import asyncio
import hmac
import json
import logging
import time
from typing import Optional
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("crackedalert.alert_status")


class ActiveAlert:
    """In-memory "an alert is firing right now" state shared with main()."""

    def __init__(self) -> None:
        self._active = False
        self._since: float = 0.0
        self._detail: str = ""

    def set(self, detail: str) -> None:
        self._active = True
        self._since = time.time()
        self._detail = detail
        log.info("alert-status: active set (%s)", detail[:60])

    def clear(self) -> None:
        if not self._active:
            return
        self._active = False
        self._since = 0.0
        self._detail = ""
        log.info("alert-status: active cleared")

    @property
    def active(self) -> bool:
        return self._active

    @property
    def since(self) -> float:
        return self._since

    @property
    def detail(self) -> str:
        return self._detail


def _json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _make_response(status: int, body: bytes) -> bytes:
    reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized",
              404: "Not Found", 500: "Internal Server Error"}.get(status, "OK")
    crlf = "\r\n"
    head = (
        f"HTTP/1.1 {status} {reason}{crlf}"
        "Content-Type: application/json; charset=utf-8"
        f"{crlf}Connection: close{crlf}"
        f"Content-Length: {len(body)}{crlf}{crlf}"
    )
    return head.encode("utf-8") + body


class AlertStatusServer:
    """An asyncio TCP server answering the alert-status endpoints."""

    def __init__(self, token: str, active: ActiveAlert,
                 host: str = "127.0.0.1", port: int = 8190) -> None:
        self._token = token
        self._active = active
        self._host = host
        self._port = port
        self._server: Optional[asyncio.AbstractServer] = None

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            try:
                method, target, _version = request_line.decode(
                    "latin-1").split(None, 2)[:3]
            except (ValueError, IndexError):
                await self._respond(writer, 400, _json({"error": "bad request"}))
                return

            # Consume and discard remaining request headers (and any small body).
            while True:
                line = await reader.readline()
                if line in (b"", b"\r\n", b"\n"):
                    break
                if len(line) > 8192:
                    break

            await self._dispatch(writer, method, target)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, writer, method: str, target: str) -> None:
        parsed = urlsplit(target)
        token = self._query_param(parsed.query, "token")
        if not token or not self._token \
                or not hmac.compare_digest(token, self._token):
            await self._respond(writer, 401, _json({"error": "unauthorized"}))
            return

        path = parsed.path.rstrip("/")
        if method == "GET" and path == "/alert-status":
            if self._active.active:
                body = _json({"active": True,
                              "since": int(self._active.since),
                              "detail": self._active.detail})
            else:
                body = _json({"active": False})
            await self._respond(writer, 200, body)
        elif method == "POST" and path == "/ack":
            self._active.clear()
            await self._respond(writer, 200, _json({"ok": True}))
        else:
            await self._respond(writer, 404, _json({"error": "not found"}))

    async def _respond(self, writer, status: int, body: bytes) -> None:
        try:
            writer.write(_make_response(status, body))
            await writer.drain()
        except (ConnectionError, asyncio.TimeoutError):
            pass

    async def start(self) -> None:
        if not self._token:
            log.warning("alert-status endpoint disabled: no ALERT_STATUS_TOKEN")
            return
        self._server = await asyncio.start_server(
            self._handle, self._host, self._port)
        log.info("alert-status listening on %s:%s", self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass

    @staticmethod
    def _query_param(query: str, name: str) -> str:
        vals = parse_qs(query, keep_blank_values=True).get(name)
        if not vals:
            return ""
        return vals[0] or ""


def build(settings) -> "AlertStatusServer":
    """Factory matching the Settings shape; endpoint disabled if no token."""
    token = getattr(settings, "alert_status_token", "") or ""
    port = getattr(settings, "alert_status_port", None) or 8190
    return AlertStatusServer(token=token, port=int(port),
                             active=ActiveAlert())
