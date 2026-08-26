"""CandleFeed polling regression tests (the one-candle-late bug).

Covers _poll_key: when a new (completed) bar appears, the feed must
dispatch the NEWEST bar (bars[-1], the just-closed one), not the previous
latest bar (which is already a candle older).
"""

import asyncio
import unittest
from unittest import mock

from crackedalert.ctrader.candles import CandleFeed


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class CandleFeedKeySyncTests(unittest.TestCase):
    def test_sync_keys_prunes_stale_keys(self):
        feed = CandleFeed(cli=None, market=None, account_id=1, engine=None)
        feed.add_symbol("XAUUSD", "M15")
        feed.add_symbol("EURUSD", "H1")
        feed.sync_keys([("XAUUSD", "M15")])
        self.assertEqual(feed.symbols(), {("XAUUSD", "M15")})

    def test_sync_keys_keeps_all_wanted(self):
        feed = CandleFeed(cli=None, market=None, account_id=1, engine=None)
        feed.add_symbol("XAUUSD", "M5")
        feed.add_symbol("EURUSD", "H1")
        feed.sync_keys([("xauusd", "m5"), ("EURUSD", "H1")])
        self.assertEqual(feed.symbols(),
                         {("XAUUSD", "M5"), ("EURUSD", "H1")})
        feed.sync_keys([("XAUUSD", "M5")])
        self.assertEqual(feed.symbols(), {("XAUUSD", "M5")})


def bar(ts, low, delta_close):
    return {
        "utcTimestampInMinutes": ts,
        "low": low,
        "deltaClose": delta_close,
    }


# low/deltaClose are scaled by PRICE_SCALE (100000):
#   close = (low + deltaClose) / 100000
B600 = bar(600, 239000000, 1000000)   # 2400.00
B605 = bar(605, 240000000, 1000000)   # 2410.00
B610 = bar(610, 241000000, 1000000)   # 2420.00
B615 = bar(615, 242000000, 1000000)   # 2430.00


class CandleFeedPollTests(unittest.TestCase):
    def setUp(self):
        self.closed = []

        class FakeEngine:
            def __init__(self, closed):
                self._closed = closed

            async def on_closed_bar(self, symbol, timeframe, close, ts):
                self._closed.append((symbol, timeframe, close, ts))

        self.feed = CandleFeed(cli=mock.Mock(), market=mock.Mock(),
                               account_id=1,
                               engine=FakeEngine(self.closed))

    def test_dispatch_newest_closed_bar_not_previous(self):
        # First poll establishes the baseline latest bar (10:10 / ts 610).
        self.feed._fetch_bars = mock.AsyncMock(
            return_value=[B600, B605, B610])
        run(self.feed._poll_key("XAUUSD", "M5"))
        self.assertEqual(self.closed, [])

        # Second poll: a new bar (10:15 / ts 615) completed. The just-closed
        # bar is the NEW latest (615), not the 10:10 bar (610). Regression
        # for the one-candle-late bug.
        self.feed._fetch_bars = mock.AsyncMock(
            return_value=[B605, B610, B615])
        run(self.feed._poll_key("XAUUSD", "M5"))
        self.assertEqual(len(self.closed), 1)
        symbol, timeframe, close, ts = self.closed[0]
        self.assertEqual((symbol, timeframe, ts), ("XAUUSD", "M5", 615))
        self.assertEqual(close, 2430.00)

    def test_no_dispatch_when_no_new_bar(self):
        self.feed._fetch_bars = mock.AsyncMock(
            return_value=[B600, B605, B610])
        run(self.feed._poll_key("XAUUSD", "M5"))
        run(self.feed._poll_key("XAUUSD", "M5"))   # same frame again
        self.assertEqual(self.closed, [])


if __name__ == "__main__":
    unittest.main()