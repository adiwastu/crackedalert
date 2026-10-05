"""Dead-grant recovery: code extraction, consent URL, code exchange,
and the rate-limited self-heal refresh wrapper.

Regression context: on 2026-10-03 cTrader revoked the bot's grant while
idle (the refresh token died unused), and the bot had no way to tell the
operator or to recover without SSH. These cover the pieces that make the
recovery chat-driven.
"""

import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

from crackedalert.ctrader import tokens as tok


class _FakeResp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class ExtractCodeTest(unittest.TestCase):
    def test_full_redirect_url(self):
        url = ("https://hotland3x3.my.id/sable/callback"
               "?code=abc123&sessionId=9")
        self.assertEqual(tok.extract_code(url), "abc123")

    def test_code_first_query_param(self):
        self.assertEqual(
            tok.extract_code("https://x/sable/callback?code=zzz"), "zzz")

    def test_url_wrapped_in_other_text(self):
        self.assertEqual(
            tok.extract_code("sure: https://x/cb?code=q1w2e3 thanks"),
            "q1w2e3")

    def test_bare_query_fragment(self):
        self.assertEqual(tok.extract_code("code=bare1"), "bare1")

    def test_no_code(self):
        self.assertIsNone(tok.extract_code("just chatting"))
        self.assertIsNone(tok.extract_code(""))
        self.assertIsNone(tok.extract_code(None))

    def test_lookalike_words_do_not_match(self):
        self.assertIsNone(tok.extract_code("decode=abc"))
        self.assertIsNone(tok.extract_code("xcode=abc"))


class AuthorizeUrlTest(unittest.TestCase):
    def test_carries_client_redirect_and_scope(self):
        url = tok.authorize_url("cid123", "https://cb.example/redirect")
        self.assertTrue(url.startswith(tok.AUTHORIZE_ENDPOINT))
        self.assertIn("client_id=cid123", url)
        self.assertIn("scope=trading", url)
        self.assertIn("product=web", url)
        self.assertIn("redirect_uri=https%3A%2F%2Fcb.example%2Fredirect",
                      url)


class ExchangeCodeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "tokens.json")
        with open(self.path, "w") as f:
            json.dump({"accessToken": "old-a", "refreshToken": "old-r",
                       "obtainedAt": 1, "expiresAt": 2}, f)
        self.store = tok.TokenStore(self.path, "cid", "secret")

    def test_exchange_persists_and_adopts_new_pair(self):
        payload = {"accessToken": "new-a", "refreshToken": "new-r",
                   "expiresIn": 2592000}

        async def scenario():
            with mock.patch("urllib.request.urlopen",
                            return_value=_FakeResp(payload)):
                await self.store.exchange_code(
                    "one-time-code", "https://cb.example/redirect")

        asyncio.run(scenario())
        self.assertEqual(self.store.access_token, "new-a")
        with open(self.path) as f:
            saved = json.load(f)
        self.assertEqual(saved["accessToken"], "new-a")
        self.assertEqual(saved["refreshToken"], "new-r")
        self.assertEqual(saved["expiresAt"] - saved["obtainedAt"], 2592000)

    def test_rejection_leaves_stored_tokens_untouched(self):
        async def scenario():
            with mock.patch("urllib.request.urlopen",
                            return_value=_FakeResp({"errorCode":
                                                    "ACCESS_DENIED"})):
                with self.assertRaises(tok.TokenError):
                    await self.store.exchange_code(
                        "bad-code", "https://cb.example/redirect")

        asyncio.run(scenario())
        self.assertEqual(self.store.access_token, "old-a")
        with open(self.path) as f:
            self.assertEqual(json.load(f)["refreshToken"], "old-r")

    def test_concurrent_refreshes_share_one_http_call(self):
        """The live and demo clients can hit their self-heal in the same
        second. The refresh token is single-use: two concurrent rotations
        with the same token would race the gateway and can burn the
        grant. Concurrent callers must share one in-flight rotation."""
        calls = {"n": 0}

        def fake_urlopen(req, timeout=30):
            calls["n"] += 1
            return _FakeResp({"accessToken": "a2", "refreshToken": "r2",
                              "expiresIn": 2592000})

        async def scenario():
            with mock.patch("urllib.request.urlopen",
                            side_effect=fake_urlopen):
                await asyncio.gather(self.store.refresh(),
                                     self.store.refresh())

        asyncio.run(scenario())
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.store.access_token, "a2")

    def test_refresh_retries_after_a_failed_attempt(self):
        """A failed rotation must not wedge the store: the next caller
        gets a fresh in-flight task, not the dead one's exception."""
        state = {"fail": True}

        def fake_urlopen(req, timeout=30):
            if state["fail"]:
                raise OSError("gateway down")
            return _FakeResp({"accessToken": "a3", "refreshToken": "r3",
                              "expiresIn": 2592000})

        async def scenario():
            with mock.patch("urllib.request.urlopen",
                            side_effect=fake_urlopen):
                with self.assertRaises(OSError):
                    await self.store.refresh()
                state["fail"] = False
                await self.store.refresh()

        asyncio.run(scenario())
        self.assertEqual(self.store.access_token, "a3")


class SelfhealRefreshTest(unittest.TestCase):
    def test_notifies_once_then_rate_limits(self):
        async def scenario():
            calls = {"n": 0}

            async def failing_refresh():
                raise RuntimeError("ACCESS_DENIED")

            async def on_dead(e):
                calls["n"] += 1

            refresh = tok.make_selfheal_refresh(
                mock.Mock(refresh=failing_refresh), on_dead,
                min_interval=3600)
            for _ in range(3):
                with self.assertRaises(RuntimeError):
                    await refresh()
            self.assertEqual(calls["n"], 1)

        asyncio.run(scenario())

    def test_notify_failure_does_not_mask_refresh_error(self):
        async def scenario():
            async def failing_refresh():
                raise RuntimeError("boom")

            async def broken_on_dead(e):
                raise ValueError("notify exploded")

            refresh = tok.make_selfheal_refresh(
                mock.Mock(refresh=failing_refresh), broken_on_dead,
                min_interval=0)
            with self.assertRaises(RuntimeError):
                await refresh()

        asyncio.run(scenario())

    def test_success_does_not_notify(self):
        async def scenario():
            calls = {"n": 0}

            async def healthy_refresh():
                pass

            async def on_dead(e):
                calls["n"] += 1

            refresh = tok.make_selfheal_refresh(
                mock.Mock(refresh=healthy_refresh), on_dead,
                min_interval=0)
            await refresh()
            self.assertEqual(calls["n"], 0)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
