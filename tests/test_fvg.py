"""Fair-value-gap detection tests (see fvg.py).

Uses the same made-up OHLC shapes as the FVG explanation: a bullish
gap on XAUUSD (2400s) and a bearish gap on EURUSD (1.08s).
"""

import unittest

from crackedalert.fvg import fresh_imbalance

SCALE = 100000


def bar(ts, low, delta_high, delta_close=0):
    return {
        "utcTimestampInMinutes": ts,
        "low": low,
        "deltaHigh": delta_high,
        "deltaClose": delta_close,
    }


class FreshImbalanceTests(unittest.TestCase):

    def test_bullish_gap(self):
        # 2400.00/2402.00/2398.50/2401.50, impulse, 2405.50/2408.00/2404.50
        bars = [
            bar(100, 239850000, 350000),    # low 2398.50 high 2402.00
            bar(160, 240100000, 500000),    # low 2401.00 high 2406.00
            bar(220, 240450000, 350000),    # low 2404.50 high 2408.00
        ]
        self.assertEqual(fresh_imbalance(bars), "bullish")

    def test_bearish_gap(self):
        # 1.0900/1.0908/1.0880/1.0885, impulse, 1.0855/1.0862/1.0840
        bars = [
            bar(300, 108800000, 280000),    # low 1.0880 high 1.0908
            bar(360, 108500000, 400000),    # low 1.0850 high 1.0890
            bar(420, 108400000, 220000),    # low 1.0840 high 1.0862
        ]
        self.assertEqual(fresh_imbalance(bars), "bearish")

    def test_overlapping_candles_no_gap(self):
        # third candle overlaps the first (low 2401.00 < high 2402.00)
        bars = [
            bar(100, 239800000, 400000),    # low 2398.00 high 2402.00
            bar(160, 240000000, 400000),
            bar(220, 240100000, 400000),    # low 2401.00 high 2405.00
        ]
        self.assertIsNone(fresh_imbalance(bars))

    def test_too_few_bars(self):
        self.assertIsNone(fresh_imbalance(
            [bar(100, 239850000, 350000), bar(160, 240100000, 500000)]))
        self.assertIsNone(fresh_imbalance([]))
        self.assertIsNone(fresh_imbalance(None))

    def test_non_consecutive_bars_ignored(self):
        # ts3 - ts1 != 120 minutes: not an H1 triplet
        bars = [
            bar(100, 239850000, 350000),
            bar(160, 240100000, 500000),
            bar(260, 240450000, 350000),
        ]
        self.assertIsNone(fresh_imbalance(bars))


if __name__ == "__main__":
    unittest.main()
