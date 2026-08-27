"""Fair-value-gap detection tests (see fvg.py).

Uses the same made-up OHLC shapes as the FVG explanation: a bullish
gap on XAUUSD (2400s) and a bearish gap on EURUSD (1.08s).
"""

import unittest

from crackedalert.alerts import (CANDLE_ABOVE, CANDLE_BELOW,
                                 CROSSING_DOWN, CROSSING_UP)
from crackedalert.fvg import (IMBALANCE_ALERT_SPECS, candle_high,
                              candle_low, fresh_imbalance)

SCALE = 100000


def bar(ts, low, delta_high, delta_close=0):
    return {
        "utcTimestampInMinutes": ts,
        "low": low,
        "deltaHigh": delta_high,
        "deltaClose": delta_close,
    }


class CandleLevelTests(unittest.TestCase):
    def test_candle_high_and_low(self):
        b = bar(100, 239850000, 350000)     # low 2398.50 high 2402.00
        self.assertAlmostEqual(candle_low(b), 2398.50)
        self.assertAlmostEqual(candle_high(b), 2402.00)


class ImbalanceAlertSpecTests(unittest.TestCase):
    """The auto-alert table for a fresh imbalance (both --all)."""

    def test_bullish_creates_two_alerts(self):
        specs = IMBALANCE_ALERT_SPECS["bullish"]
        self.assertEqual(len(specs), 2)
        kind, level, direction, note = specs[0]
        self.assertEqual((kind, level, direction),
                         ("price", "high1", CROSSING_DOWN))
        self.assertEqual(note, "masuk DH1. WATCH!")
        kind, level, direction, note = specs[1]
        self.assertEqual((kind, level, direction),
                         ("candle", "low1", CANDLE_BELOW))
        self.assertEqual(note, "strike 1 of FLIP to the DOWNSIDE. WATCH!")

    def test_bearish_creates_two_alerts(self):
        specs = IMBALANCE_ALERT_SPECS["bearish"]
        self.assertEqual(len(specs), 2)
        kind, level, direction, note = specs[0]
        self.assertEqual((kind, level, direction),
                         ("price", "low1", CROSSING_UP))
        self.assertEqual(note, "masuk S H1. WATCH!")
        kind, level, direction, note = specs[1]
        self.assertEqual((kind, level, direction),
                         ("candle", "high1", CANDLE_ABOVE))
        self.assertEqual(note, "strike 1 of FLIP to the UPSIDE. WATCH!")

    def test_all_specs_reference_existing_levels_and_directions(self):
        for specs in IMBALANCE_ALERT_SPECS.values():
            for kind, level, direction, note in specs:
                self.assertIn(kind, ("price", "candle"))
                self.assertIn(level, ("high1", "low1"))
                self.assertIn(direction,
                              (CROSSING_UP, CROSSING_DOWN,
                               CANDLE_ABOVE, CANDLE_BELOW))
                self.assertTrue(note)


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
