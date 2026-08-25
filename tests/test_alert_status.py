"""Contract tests for the alert-status HTTP endpoint (mobile alarm app).

Covers the GET /alert-status and POST /ack routes: auth (401 on missing/
wrong token), the active -> ack -> inactive state machine, and that an idle
endpoint reports active:false. Tests talk to the in-process asyncio server
over a raw socket (asyncio.open_connection) because the hand-rolled server
does not interoperate with urllib/http.client on all hosts.
"""

import asyncio
import json
import unittest

from crackedalert.alert_status import ActiveAlert, AlertStatusServer


async def _raw(port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(payload)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout=5)
        return data
    finally:
        writer.close()
        await writer.wait_closed()


def _status(data: bytes) -> int:
    return int(data.split(b" ", 2)[1])


def _body(data: bytes) -> dict:
    return json.loads(data.split(b"\r\n\r\n", 1)[1].decode("utf-8"))


class AlertStatusEndpointTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.active = ActiveAlert()

    def setUp(self) -> None:
        self.active.clear()
        self.port = 18991 + (self._testMethodName.__len__() % 40)
        self.server = AlertStatusServer(token="sekrit", active=self.active,
                                        port=self.port)
        self.loop = asyncio.new_event_loop()
        self.loop.run_until_complete(self.server.start())
        self.addCleanup(self._close)

    def _close(self) -> None:
        self.loop.run_until_complete(self.server.stop())
        self.loop.close()

    def _get(self, token: str) -> tuple:
        payload = (f"GET /alert-status?token={token} HTTP/1.1\r\n"
                   "Host: h\r\nConnection: close\r\n\r\n").encode()
        data = self.loop.run_until_complete(_raw(self.port, payload))
        return _status(data), _body(data)

    def _get_h(self, token: str) -> tuple:
        payload = (f"GET /alert-status HTTP/1.1\r\n"
                   f"Host: h\r\nX-Alert-Token: {token}\r\n"
                   "Connection: close\r\n\r\n").encode()
        data = self.loop.run_until_complete(_raw(self.port, payload))
        return _status(data), _body(data)

    def _ack_h(self, token: str) -> tuple:
        payload = (f"POST /ack HTTP/1.1\r\n"
                   f"Host: h\r\nX-Alert-Token: {token}\r\n"
                   "Connection: close\r\n\r\n").encode()
        data = self.loop.run_until_complete(_raw(self.port, payload))
        return _status(data), _body(data)

    def _ack(self, token: str) -> tuple:
        payload = (f"POST /ack?token={token} HTTP/1.1\r\n"
                   "Host: h\r\nConnection: close\r\n\r\n").encode()
        data = self.loop.run_until_complete(_raw(self.port, payload))
        return _status(data), _body(data)

    def test_idle_reports_inactive(self) -> None:
        status, body = self._get("sekrit")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"active": False})

    def test_requires_token(self) -> None:
        status, body = self._get("")
        self.assertEqual(status, 401)
        self.assertEqual(body, {"error": "unauthorized"})

    def test_wrong_token_rejected(self) -> None:
        status, body = self._get("nope")
        self.assertEqual(status, 401)
        self.assertEqual(body, {"error": "unauthorized"})

    def test_active_then_ack_then_inactive(self) -> None:
        self.active.set("SL hit for trade 42")
        status, body = self._get("sekrit")
        self.assertEqual(status, 200)
        self.assertTrue(body["active"])
        self.assertEqual(body["detail"], "SL hit for trade 42")
        self.assertIn("since", body)

        status, body = self._ack("sekrit")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})

        status, body = self._get("sekrit")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"active": False})

    def test_ack_requires_token(self) -> None:
        self.active.set("whatever")
        status, body = self._ack("")
        self.assertEqual(status, 401)
        # still active after a failed ack
        _, b = self._get("sekrit")
        self.assertTrue(b["active"])

    def test_header_token_accepted(self) -> None:
        status, body = self._get_h("sekrit")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"active": False})

    def test_header_token_required(self) -> None:
        status, body = self._get_h("")
        self.assertEqual(status, 401)
        self.assertEqual(body, {"error": "unauthorized"})

    def test_header_wrong_token_rejected(self) -> None:
        status, body = self._get_h("nope")
        self.assertEqual(status, 401)

    def test_ack_with_header_token(self) -> None:
        self.active.set("SL hit for trade 42")
        status, body = self._ack_h("sekrit")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})
        _, b = self._get("sekrit")
        self.assertFalse(b["active"])

    def test_unknown_route_404(self) -> None:
        payload = (b"GET /nope?token=sekrit HTTP/1.1\r\n"
                   b"Host: h\r\nConnection: close\r\n\r\n")
        data = self.loop.run_until_complete(_raw(self.port, payload))
        self.assertEqual(_status(data), 404)


if __name__ == "__main__":
    unittest.main()
