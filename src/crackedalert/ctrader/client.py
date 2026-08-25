"""cTrader Open API client -- JSON mode over WebSocket (port 5036).

One CTraderClient per environment (live/demo). Owns the connection,
the app-auth handshake, the protocol heartbeat, reconnection with
backoff, and clientMsgId request/response correlation. Account auth
and subscriptions are the owner's job via the on_connected callback
(they must be redone after every reconnect).

Payload type numbers are the ProtoOA* enum values from
github.com/spotware/openapi-proto-messages (identical in JSON mode).
"""

import asyncio
import collections
import itertools
import json
import logging
import ssl
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import websockets

log = logging.getLogger("crackedalert.ctrader")

HOSTS = {
    "live": "wss://live.ctraderapi.com:5036",
    "demo": "wss://demo.ctraderapi.com:5036",
}

# --- payload types ---
PT_HEARTBEAT = 51
PT_APPLICATION_AUTH_REQ = 2100
PT_APPLICATION_AUTH_RES = 2101
PT_ACCOUNT_AUTH_REQ = 2102
PT_ACCOUNT_AUTH_RES = 2103
PT_NEW_ORDER_REQ = 2106
PT_CANCEL_ORDER_REQ = 2108
PT_AMEND_POSITION_SLTP_REQ = 2110
PT_CLOSE_POSITION_REQ = 2111
PT_SYMBOLS_LIST_REQ = 2114
PT_SYMBOLS_LIST_RES = 2115
PT_SYMBOL_BY_ID_REQ = 2116
PT_SYMBOL_BY_ID_RES = 2117
PT_TRADER_REQ = 2121
PT_TRADER_RES = 2122
PT_RECONCILE_REQ = 2124
PT_RECONCILE_RES = 2125
PT_EXECUTION_EVENT = 2126
PT_SUBSCRIBE_SPOTS_REQ = 2127
PT_SUBSCRIBE_SPOTS_RES = 2128
PT_SPOT_EVENT = 2131
PT_ORDER_ERROR_EVENT = 2132
PT_GET_TRENDBARS_REQ = 2137
PT_GET_TRENDBARS_RES = 2138
PT_ERROR_RES = 2142
PT_GET_ACCOUNT_LIST_REQ = 2149
PT_GET_ACCOUNT_LIST_RES = 2150

HEARTBEAT_INTERVAL = 8.0        # server requires <=10s
BACKOFF_START = 1.0
BACKOFF_CAP = 60.0
REQUEST_TIMEOUT = 10.0


class CTraderError(Exception):
    """Protocol-level error response (ProtoOAErrorRes / OrderErrorEvent)."""

    def __init__(self, error_code: str, description: str):
        self.error_code = error_code
        self.description = description
        super().__init__("%s: %s" % (error_code, description))


class NotConnected(Exception):
    pass


EventHandler = Callable[[dict], Awaitable[None]]


class CTraderClient:
    def __init__(self, environment: str, client_id: str, client_secret: str,
                 on_connected: Optional[Callable[[], Awaitable[None]]] = None):
        if environment not in HOSTS:
            raise ValueError("environment must be live|demo")
        self.environment = environment
        self._url = HOSTS[environment]
        self._client_id = client_id
        self._client_secret = client_secret
        self._on_connected = on_connected

        self._ws = None
        self._msg_ids = itertools.count(1)
        self._pending: Dict[str, asyncio.Future] = {}
        self._handlers: Dict[int, List[EventHandler]] = {}
        self._ready = asyncio.Event()
        self._stopped = False
        self._runner: Optional[asyncio.Task] = None
        self._req_lock = asyncio.Lock()
        # (payloadType, clientMsgId, payload keys) of the last few frames;
        # dumped into timeout errors so a silent gateway is diagnosable.
        self._recent_frames = collections.deque(maxlen=10)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._runner is None:
            self._runner = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._ws is not None:
            await self._ws.close()
        if self._runner is not None:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
            self._runner = None

    async def wait_ready(self, timeout: float = 30.0) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout)

    @property
    def connected(self) -> bool:
        return self._ready.is_set()

    def add_event_handler(self, payload_type: int, handler: EventHandler) -> None:
        self._handlers.setdefault(payload_type, []).append(handler)

    def set_on_connected(self, cb: Callable[[], Awaitable[None]]) -> None:
        """Late-bind the post-auth callback (wiring order convenience)."""
        self._on_connected = cb

    # ------------------------------------------------------------------
    # requests
    # ------------------------------------------------------------------
    async def request(self, payload_type: int, payload: dict,
                      timeout: float = REQUEST_TIMEOUT) -> Tuple[int, dict]:
        """Send a frame, await the correlated response.

        Returns (payloadType, payload). Raises CTraderError on protocol
        errors, NotConnected if the link is down or drops mid-flight.

        Requests are serialized per client (never two in flight): the
        bot's candle poll (every 10s), entry-alert confirm reconciles,
        and /m flows share one connection, and the gateway has been
        observed dropping a request sent while another is outstanding.
        """
        async with self._req_lock:
            return await self._request_unlocked(payload_type, payload,
                                                timeout)

    async def _request_unlocked(self, payload_type: int, payload: dict,
                                timeout: float) -> Tuple[int, dict]:
        ws = self._ws
        if ws is None:
            raise NotConnected("cTrader %s link is down" % self.environment)
        msg_id = "ca-%d" % next(self._msg_ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await ws.send(json.dumps({
                "clientMsgId": msg_id,
                "payloadType": payload_type,
                "payload": payload,
            }))
            try:
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                raise CTraderError(
                    "TIMEOUT",
                    "no response for payloadType %d within %.0fs "
                    "(recent frames: %s)"
                    % (payload_type, timeout, list(self._recent_frames)))
        finally:
            self._pending.pop(msg_id, None)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    async def _run(self) -> None:
        backoff = BACKOFF_START
        ctx = ssl.create_default_context()
        while not self._stopped:
            try:
                # ping_interval=None: the cTrader gateway does not answer
                # WebSocket-protocol keepalive pings -- its liveness protocol
                # is the app-level ProtoOA heartbeat we send every 8s. The
                # websockets library's default keepalive (ping every 20s,
                # 20s timeout) therefore killed the connection every
                # ~10-35 min with 1011 "keepalive ping timeout", and requests
                # sent during the dead-but-not-closed window (e.g. the
                # balance TRADER request) were silently dropped.
                async with websockets.connect(self._url, ssl=ctx,
                                              ping_interval=None) as ws:
                    self._ws = ws
                    log.info("[%s] connected, authenticating app",
                             self.environment)
                    await self._app_auth()
                    backoff = BACKOFF_START
                    loop = asyncio.get_running_loop()
                    heartbeat = loop.create_task(self._heartbeat())
                    # The recv loop MUST be running before on_connected: that
                    # callback issues requests (account auth, subscriptions)
                    # whose responses only the recv loop can deliver. Starting
                    # it afterwards deadlocks every request until it times out.
                    receiver = loop.create_task(self._recv_loop(ws))
                    try:
                        if self._on_connected is not None:
                            await self._with_receiver(
                                self._on_connected(), receiver)
                        self._ready.set()
                        log.info("[%s] ready", self.environment)
                        await receiver
                    finally:
                        heartbeat.cancel()
                        receiver.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] connection lost: %s: %s", self.environment,
                           type(e).__name__, e or "(no message)")
            finally:
                self._ready.clear()
                self._ws = None
                self._fail_pending(NotConnected("connection dropped"))
            if not self._stopped:
                log.info("[%s] reconnecting in %.0fs", self.environment,
                         backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_CAP)

    @staticmethod
    async def _with_receiver(setup_coro, receiver: asyncio.Task) -> None:
        """Await setup work, but bail out immediately if the connection
        dies underneath it instead of waiting for request timeouts."""
        setup = asyncio.ensure_future(setup_coro)
        done, _ = await asyncio.wait({setup, receiver},
                                     return_when=asyncio.FIRST_COMPLETED)
        if receiver in done:
            setup.cancel()
            await receiver          # re-raise whatever killed the link
            raise NotConnected("connection closed during setup")
        await setup                 # propagate setup failures

    async def _app_auth(self) -> None:
        # Cannot use request(): recv loop is not running yet.
        await self._ws.send(json.dumps({
            "clientMsgId": "app-auth",
            "payloadType": PT_APPLICATION_AUTH_REQ,
            "payload": {
                "clientId": self._client_id,
                "clientSecret": self._client_secret,
            },
        }))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + REQUEST_TIMEOUT
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CTraderError(
                    "TIMEOUT", "app auth timed out waiting for response")
            try:
                raw = await asyncio.wait_for(self._ws.recv(), remaining)
            except asyncio.TimeoutError:
                # bare TimeoutError has no message -- give it one so
                # "connection lost" logs are diagnosable, not blank.
                raise CTraderError(
                    "TIMEOUT", "app auth timed out waiting for response")
            frame = json.loads(raw)
            pt = frame.get("payloadType")
            if pt == PT_APPLICATION_AUTH_RES:
                return
            if pt == PT_ERROR_RES:
                p = frame.get("payload", {})
                raise CTraderError(p.get("errorCode", "?"),
                                   p.get("description", "app auth failed"))
            # ignore anything else (heartbeats etc.)

    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except (ValueError, TypeError):
                log.warning("[%s] non-JSON frame ignored", self.environment)
                continue
            pt = frame.get("payloadType")
            payload = frame.get("payload", {}) or {}
            msg_id = frame.get("clientMsgId")
            self._recent_frames.append(
                (pt, msg_id, tuple(sorted(payload.keys()))[:6]))

            fut = self._pending.get(msg_id) if msg_id else None
            if fut is not None and not fut.done():
                if pt in (PT_ERROR_RES, PT_ORDER_ERROR_EVENT):
                    fut.set_exception(CTraderError(
                        payload.get("errorCode", "?"),
                        payload.get("description", "request failed")))
                else:
                    fut.set_result((pt, payload))
                # correlated frames are also offered to event handlers below
                # (execution events matter to both the requester and stream
                # consumers); uncorrelated handlers must tolerate that.

            for handler in self._handlers.get(pt, []):
                try:
                    await handler(payload)
                except Exception:
                    log.exception("[%s] event handler failed for pt=%s",
                                  self.environment, pt)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            ws = self._ws
            if ws is None:
                return
            try:
                await ws.send(json.dumps(
                    {"payloadType": PT_HEARTBEAT, "payload": {}}))
            except Exception:
                return

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
