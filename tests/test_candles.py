"""CandleFeed polling regression tests (the one-candle-late bug).

Covers _poll_key: when a new (completed) bar appears, the feed must
dispatch the NEWEST bar (bars[-1], the just-closed one), not the previous
latest bar (which is already a candle older).
"""

import asyncio
import unittest
from unittest import mock

from crackedalert.ctrader.candles import HISTORY_COUNT, CandleFeed


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


def bar(ts, low, delta_close, delta_open=0, delta_high=None):
    raw = {
        "utcTimestampInMinutes": ts,
        "low": low,
        "deltaOpen": delta_open,
        "deltaClose": delta_close,
        "deltaHigh": delta_close if delta_high is None else delta_high,
    }
    return raw


# low and the deltas are scaled by PRICE_SCALE (100000), and the deltas
# are measured up from the low: close = (low + deltaClose) / 100000.
B600 = bar(600, 239000000, 1000000)   # 2390.00 / 2400.00
B605 = bar(605, 240000000, 1000000)   # 2400.00 / 2410.00
B610 = bar(610, 241000000, 1000000)   # 2410.00 / 2420.00
B615 = bar(615, 242000000, 1000000)   # 2420.00 / 2430.00


class CandleFeedPollTests(unittest.TestCase):
    def setUp(self):
        self.closed = []

        class FakeEngine:
            def __init__(self, closed):
                self._closed = closed

            async def on_closed_bar(self, symbol, timeframe, bar):
                self._closed.append((symbol, timeframe, bar))

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
        symbol, timeframe, bar = self.closed[0]
        self.assertEqual((symbol, timeframe, bar.ts), ("XAUUSD", "M5", 615))
        self.assertEqual(bar.close, 2430.00)

    def test_no_dispatch_when_no_new_bar(self):
        self.feed._fetch_bars = mock.AsyncMock(
            return_value=[B600, B605, B610])
        run(self.feed._poll_key("XAUUSD", "M5"))
        run(self.feed._poll_key("XAUUSD", "M5"))   # same frame again
        self.assertEqual(self.closed, [])


class TrendbarUnpackTests(unittest.TestCase):
    """Wire format is a low plus unsigned deltas measured up from it."""

    def test_decodes_all_four_prices(self):
        raw = bar(700, 240000000, delta_close=200000,
                  delta_open=50000, delta_high=300000)
        b = CandleFeed._unpack(raw)
        self.assertEqual(b.ts, 700)
        self.assertAlmostEqual(b.open, 2400.50)
        self.assertAlmostEqual(b.high, 2403.00)
        self.assertAlmostEqual(b.low, 2400.00)
        self.assertAlmostEqual(b.close, 2402.00)

    def test_missing_deltas_collapse_onto_the_low(self):
        b = CandleFeed._unpack({"utcTimestampInMinutes": 700,
                                "low": 240000000})
        self.assertEqual((b.open, b.high, b.low, b.close),
                         (2400.00, 2400.00, 2400.00, 2400.00))


class CandleFeedHistoryCountTests(unittest.TestCase):
    """The fetch window is a parameter, so the warm-up scan can widen it."""

    @staticmethod
    def _feed(**kw):
        cli = mock.Mock()
        cli.request = mock.AsyncMock(return_value=(0, {"trendbar": []}))
        market = mock.Mock()
        market.ensure_symbol = mock.AsyncMock(
            return_value=mock.Mock(symbol_id=42))
        feed = CandleFeed(cli=cli, market=market, account_id=1,
                          engine=None, **kw)
        return feed, cli

    @staticmethod
    def _requested_count(cli):
        return cli.request.await_args.args[1]["count"]

    def test_defaults_to_the_feed_window(self):
        feed, cli = self._feed()
        run(feed._fetch_bars("XAUUSD", "H1"))
        self.assertEqual(self._requested_count(cli), HISTORY_COUNT)

    def test_an_explicit_count_overrides_it(self):
        feed, cli = self._feed()
        run(feed._fetch_bars("XAUUSD", "H1", count=300))
        self.assertEqual(self._requested_count(cli), 300)

    def test_the_constructor_can_set_the_default(self):
        feed, cli = self._feed(history_count=1000)
        run(feed._fetch_bars("XAUUSD", "H1"))
        self.assertEqual(self._requested_count(cli), 1000)


if __name__ == "__main__":
    unittest.main()