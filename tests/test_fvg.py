"""Fair-value-gap detection tests (see fvg.py).

Uses the same made-up OHLC shapes as the FVG explanation: a bullish
gap on XAUUSD (2400s) and a bearish gap on EURUSD (1.08s).
"""

import unittest

from crackedalert.alerts import (CANDLE_ABOVE, CANDLE_BELOW,
                                 CROSSING_DOWN, CROSSING_UP)
from crackedalert.fvg import (IMBALANCE_ALERT_SPECS, ImbalanceGate,
                              back_to_back, candle_high, candle_low,
                              fresh_imbalance)

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


class BackToBackTests(unittest.TestCase):
    """Consecutive triplets overlap by two candles: one impulse, one alert."""

    # Timestamps run 600/660/720/780 so both the newest triplet and
    # the one before it are a valid H1 run (ts3 - ts1 == 120). They start
    # off zero because fresh_imbalance treats ts 0 as a missing stamp.
    def test_consecutive_imbalances_are_back_to_back(self):
        bars = [
            bar(600, 10000000, 200000),     # 100.00 / 102.00
            bar(660, 10300000, 300000),    # 103.00 / 106.00
            bar(720, 10400000, 400000),   # 104.00 / 108.00  gap over bar 0
            bar(780, 10700000, 300000),   # 107.00 / 110.00  gap over bar 1
        ]
        self.assertEqual(fresh_imbalance(bars), "bullish")
        self.assertTrue(back_to_back(bars))

    def test_an_isolated_imbalance_is_not(self):
        bars = [
            bar(600, 10000000, 500000),     # 100.00 / 105.00
            bar(660, 10100000, 500000),    # 101.00 / 106.00
            bar(720, 10200000, 500000),   # 102.00 / 107.00  overlaps bar 0
            bar(780, 10700000, 300000),   # 107.00 / 110.00  gap over bar 1
        ]
        self.assertEqual(fresh_imbalance(bars), "bullish")
        self.assertFalse(back_to_back(bars))

    def test_direction_is_not_considered(self):
        # Previous triplet gapped down, newest gapped up. Still one
        # impulse as far as the filter is concerned.
        bars = [
            bar(600, 20000000, 500000),     # 200.00 / 205.00
            bar(660, 19000000, 500000),    # 190.00 / 195.00
            bar(720, 18000000, 500000),   # 180.00 / 185.00  gap under bar 0
            bar(780, 19600000, 400000),   # 196.00 / 200.00  gap over bar 1
        ]
        self.assertEqual(fresh_imbalance(bars), "bullish")
        self.assertTrue(back_to_back(bars))

    def test_too_few_bars_does_not_suppress(self):
        # Three bars can show an imbalance but cannot show what came
        # before it. Not suppressing is the safer unknown.
        bars = [
            bar(660, 10300000, 300000),
            bar(720, 10400000, 400000),
            bar(780, 10700000, 300000),
        ]
        self.assertEqual(fresh_imbalance(bars), "bullish")
        self.assertFalse(back_to_back(bars))
        self.assertFalse(back_to_back([]))
        self.assertFalse(back_to_back(None))

    def test_no_imbalance_before_means_not_back_to_back(self):
        # Non-consecutive timestamps in the earlier triplet: not an H1
        # run, so it cannot have completed an imbalance.
        bars = [
            bar(600, 10000000, 200000),
            bar(720, 10300000, 300000),   # gap in the series
            bar(780, 10400000, 400000),
            bar(840, 10700000, 300000),
        ]
        self.assertFalse(back_to_back(bars))


class ImbalanceGateTests(unittest.TestCase):
    """Skip a continuation only if its first imbalance was really seen."""

    def test_the_first_imbalance_alerts(self):
        self.assertTrue(ImbalanceGate().admit(600))

    def test_the_next_candle_continuing_it_is_skipped(self):
        gate = ImbalanceGate()
        gate.admit(600)
        self.assertFalse(gate.admit(660))

    def test_a_long_impulse_alerts_once(self):
        gate = ImbalanceGate()
        results = [gate.admit(ts) for ts in (600, 660, 720, 780)]
        self.assertEqual(results, [True, False, False, False])

    def test_a_missed_first_hour_still_alerts_on_the_next(self):
        # The bug this replaces: the check for 600 never ran (stale data,
        # restart), so the gate first sees the impulse at 660. The market
        # had an imbalance at 600, but nothing alerted it -- this must.
        gate = ImbalanceGate()
        self.assertTrue(gate.admit(660))
        self.assertFalse(gate.admit(720))

    def test_the_same_imbalance_evaluated_twice_alerts_once(self):
        gate = ImbalanceGate()
        self.assertTrue(gate.admit(600))
        self.assertFalse(gate.admit(600))

    def test_a_gap_starts_a_new_impulse(self):
        gate = ImbalanceGate()
        gate.admit(600)
        self.assertTrue(gate.admit(720))   # a candle with no imbalance between

    def test_it_remembers_what_it_last_saw(self):
        gate = ImbalanceGate()
        self.assertIsNone(gate.last_seen)
        gate.admit(600)
        gate.admit(660)                    # skipped, but still seen
        self.assertEqual(gate.last_seen, 660)


if __name__ == "__main__":
    unittest.main()
