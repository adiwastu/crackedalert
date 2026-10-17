"""FCM push: JWT signing, message shape, token store, push_all fan-out
(see fcm.py). No network: the HTTP POST is stubbed at FcmSender._post."""

import base64
import calendar
import json
import shutil
import tempfile
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import (
    generate_private_key)

from crackedalert.fcm import (FCMSendError, FCMTokenStore, FcmSender,
                              ServiceAccount, build_message, make_jwt)


def _pem_pair():
    key = generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    public_key = key.public_key()
    return private_pem, public_key


PRIVATE_PEM, PUBLIC_KEY = _pem_pair()


def make_sa():
    return ServiceAccount(project_id="cracked-test",
                          client_email="bot@cracked-test.iam.gserviceaccount.com",
                          private_key=PRIVATE_PEM)


def b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


class MakeJwtTests(unittest.TestCase):
    def test_signature_verifies_and_claims_match(self):
        now = 1_700_000_000
        token = make_jwt(make_sa(), now)
        header_b64, claims_b64, sig_b64 = token.split(".")
        header = json.loads(b64url_decode(header_b64))
        claims = json.loads(b64url_decode(claims_b64))
        self.assertEqual(header["alg"], "RS256")
        self.assertEqual(claims["iss"],
                         "bot@cracked-test.iam.gserviceaccount.com")
        self.assertEqual(claims["scope"],
                         "https://www.googleapis.com/auth/firebase.messaging")
        self.assertEqual(claims["aud"],
                         "https://oauth2.googleapis.com/token")
        self.assertEqual(claims["iat"], now)
        self.assertEqual(claims["exp"], now + 3600)
        PUBLIC_KEY.verify(b64url_decode(sig_b64),
                          ("%s.%s" % (header_b64, claims_b64)).encode(),
                          padding.PKCS1v15(), hashes.SHA256())

    def test_signing_input_is_covered_by_the_signature(self):
        # Flipping one claims byte must invalidate the signature.
        token = make_jwt(make_sa())
        h, c, s = token.split(".")
        tampered = h + ("." + c[:-1] + ("A" if c[-1] != "A" else "B"))
        with self.assertRaises(Exception):
            PUBLIC_KEY.verify(b64url_decode(s),
                              tampered.encode(), padding.PKCS1v15(),
                              hashes.SHA256())


class BuildMessageTests(unittest.TestCase):
    def test_data_only_high_priority(self):
        msg = build_message("devtoken", "Cracked Alert",
                            "price hit 2450", 1_700_000_000.7)
        self.assertEqual(msg["message"]["token"], "devtoken")
        self.assertEqual(msg["message"]["data"]["since"], "1700000000")
        self.assertEqual(msg["message"]["data"]["detail"], "price hit 2450")
        self.assertEqual(msg["message"]["android"]["priority"], "HIGH")
        # A notification block would stop onMessageReceived from firing
        # while the app is backgrounded, killing the full-screen alarm.
        self.assertNotIn("notification", msg["message"])


class FCMTokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = FCMTokenStore(self.dir + "/test.db")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_add_is_idempotent_and_lists(self):
        self.assertTrue(self.store.add("tokA"))
        self.assertFalse(self.store.add("tokA"))
        self.store.add("tokB")
        self.assertEqual(self.store.all_tokens(), ["tokA", "tokB"])

    def test_remove(self):
        self.store.add("tokA")
        self.store.remove("tokA")
        self.assertEqual(self.store.all_tokens(), [])


class FakeSender(FcmSender):
    """Records posts; canned responses per URL substring."""

    def __init__(self, responses):
        super().__init__(make_sa(), FCMTokenStore(":memory:"))
        self.responses = responses   # substring -> (status, body bytes)
        self.posts = []

    def _post(self, url, data, headers):
        self.posts.append((url, data, headers))
        for needle, canned in self.responses.items():
            # Route on URL or body: the device token rides in the body.
            if needle in url or needle in data.decode("utf-8", "replace"):
                return canned
        return 500, b"{}"

    def close(self):
        self._store.close()


class FcmSenderTests(unittest.TestCase):
    def test_push_all_sends_one_message_per_token(self):
        sender = FakeSender({
            "oauth2": (200, json.dumps(
                {"access_token": "goog", "expires_in": 3600}).encode()),
            "messages:send": (200, b"{}"),
        })
        try:
            sender._store.add("tokA")
            sender._store.add("tokB")
            import asyncio
            asyncio.run(sender.push_all("T", "body", 123.0))
            tokens = sender._store.all_tokens()
        finally:
            sender.close()
        sends = [p for p in sender.posts if "messages:send" in p[0]]
        self.assertEqual(len(sends), 2)
        auth = sends[0][2]["Authorization"]
        self.assertEqual(auth, "Bearer goog")
        tokens = {json.loads(p[1])["message"]["token"] for p in sends}
        self.assertEqual(tokens, {"tokA", "tokB"})

    def test_unregistered_token_is_dropped(self):
        sender = FakeSender({
            "oauth2": (200, json.dumps(
                {"access_token": "goog", "expires_in": 3600}).encode()),
            "tokGONE": (404, b"{}"),
            "messages:send": (200, b"{}"),
        })
        try:
            sender._store.add("tokGONE")
            sender._store.add("tokOK")
            import asyncio
            asyncio.run(sender.push_all("T", "body", 1.0))
            tokens = sender._store.all_tokens()
        finally:
            sender.close()
        self.assertEqual(tokens, ["tokOK"])

    def test_transient_failure_keeps_the_token(self):
        sender = FakeSender({
            "oauth2": (200, json.dumps(
                {"access_token": "goog", "expires_in": 3600}).encode()),
            "messages:send": (500, b"boom"),
        })
        try:
            sender._store.add("tokA")
            import asyncio
            asyncio.run(sender.push_all("T", "body", 1.0))
            tokens = sender._store.all_tokens()
        finally:
            sender.close()
        self.assertEqual(tokens, ["tokA"])

    def test_oauth_failure_fails_soft_and_keeps_tokens(self):
        # push_all never raises (main.py fires it fire-and-forget); an
        # oauth outage must neither crash the alert path nor drop tokens.
        sender = FakeSender({"oauth2": (401, b"bad jwt")})
        try:
            sender._store.add("tokA")
            import asyncio
            asyncio.run(sender.push_all("T", "body", 1.0))
            tokens = sender._store.all_tokens()
        finally:
            sender.close()
        self.assertEqual(tokens, ["tokA"])
        self.assertEqual(
            [p for p in sender.posts if "messages:send" in p[0]], [])

    def test_access_token_is_cached(self):
        sender = FakeSender({
            "oauth2": (200, json.dumps(
                {"access_token": "goog", "expires_in": 3600}).encode()),
            "messages:send": (200, b"{}"),
        })
        try:
            sender._store.add("tokA")
            import asyncio
            asyncio.run(sender.push_all("T", "1", 1.0))
            asyncio.run(sender.push_all("T", "2", 2.0))
        finally:
            sender.close()
        oauth_posts = [p for p in sender.posts if "oauth2" in p[0]]
        self.assertEqual(len(oauth_posts), 1)


class ServiceAccountFileTests(unittest.TestCase):
    def test_load_roundtrip(self):
        import os
        import tempfile
        raw = {
            "project_id": "cracked-test",
            "client_email": "bot@cracked-test.iam.gserviceaccount.com",
            "private_key": PRIVATE_PEM,
        }
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(raw, f)
            sa = ServiceAccount.__new__(ServiceAccount)
            from crackedalert.fcm import load_service_account
            loaded = load_service_account(path)
            self.assertEqual(loaded.project_id, "cracked-test")
            self.assertEqual(loaded.client_email,
                             raw["client_email"])
        finally:
            os.unlink(path)

    def test_missing_fields_are_rejected(self):
        import os
        import tempfile
        from crackedalert.fcm import load_service_account
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"project_id": "x"}, f)
            with self.assertRaises(ValueError):
                load_service_account(path)
        finally:
            os.unlink(path)


class RegisterRouteTests(unittest.TestCase):
    """POST /register-fcm through the AlertStatusServer dispatcher."""

    def setUp(self):
        import asyncio
        from crackedalert.alert_status import ActiveAlert, AlertStatusServer
        self.asyncio = asyncio
        self.added = []

        async def register(token):
            self.added.append(token)
            return token not in self.added[:-1]

        self.server = AlertStatusServer(token="sekret",
                                        active=ActiveAlert(),
                                        register_fcm=register)
        self.server._token = "sekret"

    def _dispatch(self, method, target, body=b""):
        async def run():
            writes = []

            class FakeWriter:
                def write(self, data):
                    writes.append(data)

                async def drain(self):
                    pass

                def close(self):
                    pass

                async def wait_closed(self):
                    pass

            writer = FakeWriter()
            await self.server._dispatch(writer, method, target,
                                        {"x-alert-token": "sekret"}, body)
            head, _, payload = writes[0].partition(b"\r\n\r\n")
            return int(head.split()[1]), json.loads(payload)

        return self.asyncio.run(run())

    def test_register_stores_the_token(self):
        status, out = self._dispatch(
            "POST", "/register-fcm",
            json.dumps({"token": "devtok"}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(self.added, ["devtok"])

    def test_missing_body_token_is_a_400(self):
        status, _ = self._dispatch("POST", "/register-fcm",
                                   json.dumps({"nope": 1}).encode())
        self.assertEqual(status, 400)

    def test_route_is_token_gated(self):
        async def run():
            writes = []

            class FakeWriter:
                def write(self, data):
                    writes.append(data)

                async def drain(self):
                    pass

                def close(self):
                    pass

                async def wait_closed(self):
                    pass

            writer = FakeWriter()
            await self.server._dispatch(writer, "POST", "/register-fcm",
                                        {"x-alert-token": "wrong"}, b"{}")
            return int(writes[0].split()[1])

        self.assertEqual(self.asyncio.run(run()), 401)


if __name__ == "__main__":
    unittest.main()
