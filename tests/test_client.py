"""CTraderClient connection-lifecycle tests against a fake WebSocket.

Regression: the on_connected callback issues requests (account auth,
subscriptions) whose responses are delivered by the recv loop. An earlier
version started the recv loop only AFTER on_connected returned, so every
such request deadlocked until its 10s timeout and the client never became
ready -- it just reconnected forever. test_on_connected_may_issue_requests
fails on that structure and passes on the fixed one.
"""

import asyncio
import json
import unittest
from unittest import mock

from crackedalert.ctrader import client as ct


class FakeWS:
    """Minimal stand-in: auto-answers auth requests, supports the
    `async for` the recv loop uses."""

    def __init__(self, answer_account_auth=True):
        self.sent = []
        self._q = None
        self._closed = False
        self._answer_account_auth = answer_account_auth

    @property
    def _queue(self) -> asyncio.Queue:
        # Created lazily so it binds to the running loop: on Python 3.9 an
        # asyncio.Queue built at import/construction time attaches to the
        # then-current loop and breaks when awaited from a different one.
        if self._q is None:
            self._q = asyncio.Queue()
        return self._q

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        pt = msg.get("payloadType")
        reply_type = None
        if pt == ct.PT_APPLICATION_AUTH_REQ:
            reply_type = ct.PT_APPLICATION_AUTH_RES
        elif pt == ct.PT_ACCOUNT_AUTH_REQ and self._answer_account_auth:
            reply_type = ct.PT_ACCOUNT_AUTH_RES
        elif pt == ct.PT_TRADER_REQ:
            reply_type = ct.PT_TRADER_RES
        if reply_type is not None:
            await self._queue.put({
                "clientMsgId": msg.get("clientMsgId"),
                "payloadType": reply_type,
                "payload": {"trader": {"balance": 100000, "moneyDigits": 2}},
            })

    async def recv(self) -> str:
        return json.dumps(await self._queue.get())

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._closed:
            raise StopAsyncIteration
        return json.dumps(await self._queue.get())

    async def close(self) -> None:
        self._closed = True

    async def push_event(self, payload_type: int, payload: dict) -> None:
        await self._queue.put({"payloadType": payload_type,
                               "payload": payload})


class FakeConnect:
    """Async context manager replacing websockets.connect."""

    def __init__(self, ws):
        self._ws = ws

    def __call__(self, *_args, **_kwargs):
        return self

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *_exc):
        return False


def run(coro, timeout=5):
    async def guarded():
        return await asyncio.wait_for(coro, timeout)
    return asyncio.run(guarded())


class ConnectionLifecycle(unittest.TestCase):
    def _client(self, ws, on_connected=None):
        cli = ct.CTraderClient("demo", "cid", "secret",
                               on_connected=on_connected)
        return cli

    def test_app_auth_then_ready(self):
        ws = FakeWS()

        async def scenario():
            with mock.patch.object(ct.websockets, "connect", FakeConnect(ws)):
                cli = self._client(ws)
                cli.start()
                await cli.wait_ready(timeout=3)
                self.assertTrue(cli.connected)
                await cli.stop()

        run(scenario())
        self.assertEqual(ws.sent[0]["payloadType"],
                         ct.PT_APPLICATION_AUTH_REQ)

    def test_on_connected_may_issue_requests(self):
        """The deadlock regression: a request inside on_connected must
        resolve, and the client must reach ready."""
        ws = FakeWS()
        seen = {}

        async def scenario():
            with mock.patch.object(ct.websockets, "connect", FakeConnect(ws)):
                cli = ct.CTraderClient("demo", "cid", "secret")

                async def on_connected():
                    pt, _payload = await cli.request(
                        ct.PT_ACCOUNT_AUTH_REQ,
                        {"ctidTraderAccountId": 1, "accessToken": "t"},
                        timeout=2)
                    seen["account_auth"] = pt

                cli.set_on_connected(on_connected)
                cli.start()
                await cli.wait_ready(timeout=3)
                await cli.stop()

        run(scenario())
        self.assertEqual(seen.get("account_auth"), ct.PT_ACCOUNT_AUTH_RES)

    def test_request_timeout_names_the_payload_type(self):
        ws = FakeWS(answer_account_auth=False)

        async def scenario():
            with mock.patch.object(ct.websockets, "connect", FakeConnect(ws)):
                cli = self._client(ws)
                cli.start()
                await cli.wait_ready(timeout=3)
                with self.assertRaises(ct.CTraderError) as cm:
                    await cli.request(ct.PT_ACCOUNT_AUTH_REQ, {}, timeout=0.3)
                self.assertEqual(cm.exception.error_code, "TIMEOUT")
                self.assertIn(str(ct.PT_ACCOUNT_AUTH_REQ),
                              cm.exception.description)
                await cli.stop()

        run(scenario())

    def test_events_reach_handlers(self):
        ws = FakeWS()
        received = []

        async def scenario():
            with mock.patch.object(ct.websockets, "connect", FakeConnect(ws)):
                cli = self._client(ws)

                async def handler(payload):
                    received.append(payload)

                cli.add_event_handler(ct.PT_SPOT_EVENT, handler)
                cli.start()
                await cli.wait_ready(timeout=3)
                await ws.push_event(ct.PT_SPOT_EVENT,
                                    {"symbolId": 41, "bid": 245000000})
                await asyncio.sleep(0.2)
                await cli.stop()

        run(scenario())
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["symbolId"], 41)


if __name__ == "__main__":
    unittest.main()
