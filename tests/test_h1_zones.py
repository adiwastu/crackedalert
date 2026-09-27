"""M15 gaps nested in live H1 zones (see h1_zones.py).

Bars here are raw trendbar dicts, like the gateway sends, so nested_entry
is exercised on exactly what the M15 check hands it.
"""

import os
import shutil
import tempfile
import unittest

from crackedalert import alerts
from crackedalert.fvg import fresh_imbalance
from crackedalert.h1_zones import (H1Zone, H1ZoneStore, NestedGate,
                                   nested_entry, nesting_zone)

SCALE = 100000
FORMED = 600            # UTC minutes the H1 gap completed


def mbar(ts, low, high):
    """An M15 trendbar from real prices."""
    return {"utcTimestampInMinutes": ts, "low": round(low * SCALE),
            "deltaHigh": round((high - low) * SCALE)}


def dh1(bottom=4270.0, top=4282.0, flip="FLIP", formed=FORMED):
    return H1Zone(flip, "XAUUSD", "bullish", bottom, top, formed)


def sh1(bottom=4300.0, top=4312.0, flip="SFLP", formed=FORMED):
    return H1Zone(flip, "XAUUSD", "bearish", bottom, top, formed)


# Bullish M15 gap (4275, 4278), inside DH1 [4270, 4282], starting as the
# zone forms.
BULLISH_INSIDE = [mbar(600, 4270, 4275), mbar(615, 4274, 4285),
                  mbar(630, 4278, 4288)]
# Bearish M15 gap (4304, 4308), inside S H1 [4300, 4312].
BEARISH_INSIDE = [mbar(600, 4308, 4315), mbar(615, 4296, 4309),
                  mbar(630, 4295, 4304)]


class FixtureTests(unittest.TestCase):
    """Guard the fixtures: every case below assumes these are real FVGs."""

    def test_the_fixtures_are_m15_imbalances(self):
        self.assertEqual(fresh_imbalance(BULLISH_INSIDE, 15), "bullish")
        self.assertEqual(fresh_imbalance(BEARISH_INSIDE, 15), "bearish")


class NestedEntryTests(unittest.TestCase):

    def test_a_bullish_gap_inside_dh1_arms_a_dm15_entry(self):
        entry = nested_entry(BULLISH_INSIDE, "bullish", [dh1()])
        self.assertEqual(entry.zone.name, "DH1")
        self.assertAlmostEqual(entry.level, 4275.0)     # its candle 1 high
        self.assertEqual(entry.direction, alerts.CROSSING_DOWN)
        self.assertEqual(entry.note, "masuk DM15. WATCH!")
        self.assertAlmostEqual(entry.bottom, 4275.0)
        self.assertAlmostEqual(entry.top, 4278.0)

    def test_a_bearish_gap_inside_s_h1_arms_an_s_m15_entry(self):
        entry = nested_entry(BEARISH_INSIDE, "bearish", [sh1()])
        self.assertEqual(entry.zone.name, "S H1")
        self.assertAlmostEqual(entry.level, 4308.0)     # its candle 1 low
        self.assertEqual(entry.direction, alerts.CROSSING_UP)
        self.assertEqual(entry.note, "masuk S M15. WATCH!")

    def test_a_gap_outside_every_zone_arms_nothing(self):
        above = [mbar(600, 4290, 4295), mbar(615, 4294, 4305),
                 mbar(630, 4298, 4308)]
        self.assertEqual(fresh_imbalance(above, 15), "bullish")
        self.assertIsNone(nested_entry(above, "bullish", [dh1()]))

    def test_a_gap_pointing_the_other_way_arms_nothing(self):
        self.assertIsNone(nested_entry(BULLISH_INSIDE, "bullish", [sh1(
            bottom=4270.0, top=4282.0)]))

    def test_a_gap_from_the_h1_impulse_itself_arms_nothing(self):
        # Its first candle opened before the H1 gap had formed: this is
        # the move that created the zone, not price returning to it.
        early = [mbar(585, 4270, 4275), mbar(600, 4274, 4285),
                 mbar(615, 4278, 4288)]
        self.assertEqual(fresh_imbalance(early, 15), "bullish")
        self.assertIsNone(nested_entry(early, "bullish", [dh1()]))

    def test_no_zones_arms_nothing(self):
        self.assertIsNone(nested_entry(BULLISH_INSIDE, "bullish", []))


class NestedGateTests(unittest.TestCase):
    """Nesting is decided before the back-to-back gate."""

    # Price wicks below DH1 [4270, 4282], then bounces. The bounce's first
    # gap (4260, 4263) forms below the zone; the next (4270, 4273) lands in
    # it, one candle later; the one after (4276, 4278) continues it.
    BOUNCE = [mbar(600, 4255, 4260), mbar(615, 4259, 4270),
              mbar(630, 4263, 4276), mbar(645, 4273, 4285),
              mbar(660, 4278, 4290)]

    def window(self, end):
        """The bars the M15 check would hold with `end` as the newest."""
        bars = self.BOUNCE[:end]
        self.assertEqual(fresh_imbalance(bars, 15), "bullish")
        return bars

    def test_a_gap_below_the_zone_does_not_block_the_next_one_inside(self):
        # Gating first would mark the outside gap as seen and skip the one
        # that nests -- losing the only alert this bounce should produce.
        gate = NestedGate()
        outcome, _ = gate.evaluate(self.window(3), "bullish", [dh1()])
        self.assertEqual(outcome, NestedGate.OUTSIDE)
        outcome, entry = gate.evaluate(self.window(4), "bullish", [dh1()])
        self.assertEqual(outcome, NestedGate.ARMED)
        self.assertAlmostEqual(entry.level, 4270.0)

    def test_a_continuation_of_an_armed_gap_is_skipped(self):
        gate = NestedGate()
        gate.evaluate(self.window(4), "bullish", [dh1()])
        outcome, _ = gate.evaluate(self.window(5), "bullish", [dh1()])
        self.assertEqual(outcome, NestedGate.CONTINUATION)

    def test_an_outside_gap_is_not_recorded_as_seen(self):
        gate = NestedGate()
        gate.evaluate(self.window(3), "bullish", [dh1()])
        self.assertIsNone(gate.last_seen)


class NestingZoneTests(unittest.TestCase):
    """Overlap in any form counts: inside, crossing an edge, slicing."""

    def nests(self, bottom, top, zones=None, first_bar_ts=FORMED):
        return nesting_zone(zones or [dh1()], "bullish", bottom, top,
                            first_bar_ts) is not None

    def test_fully_inside(self):
        self.assertTrue(self.nests(4275, 4278))

    def test_crossing_the_top_edge(self):
        self.assertTrue(self.nests(4280, 4285))

    def test_crossing_the_bottom_edge(self):
        self.assertTrue(self.nests(4265, 4272))

    def test_slicing_right_through(self):
        self.assertTrue(self.nests(4265, 4290))

    def test_only_touching_an_edge_does_not_count(self):
        self.assertFalse(self.nests(4282, 4285))
        self.assertFalse(self.nests(4265, 4270))

    def test_clear_of_the_zone(self):
        self.assertFalse(self.nests(4290, 4295))

    def test_starting_exactly_as_the_zone_forms_counts(self):
        self.assertTrue(self.nests(4275, 4278, first_bar_ts=FORMED))

    def test_the_newest_matching_zone_wins(self):
        newer = dh1(flip="NEWR", formed=FORMED)
        older = dh1(flip="OLDR", formed=FORMED - 60)
        zone = nesting_zone([newer, older], "bullish", 4275, 4278, FORMED)
        self.assertEqual(zone.flip_alert_id, "NEWR")


class H1ZoneStoreTests(unittest.TestCase):

    def setUp(self):
        self.store = H1ZoneStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_zones_come_back_newest_first(self):
        self.store.add(dh1(flip="OLDR", formed=500))
        self.store.add(dh1(flip="NEWR", formed=700))
        zones = self.store.live("XAUUSD", lambda _id: True)
        self.assertEqual([z.flip_alert_id for z in zones], ["NEWR", "OLDR"])

    def test_a_zone_whose_flip_is_gone_is_dead_and_deleted(self):
        self.store.add(dh1(flip="LIVE"))
        self.store.add(dh1(flip="DEAD"))
        zones = self.store.live("XAUUSD", lambda id_: id_ != "DEAD")
        self.assertEqual([z.flip_alert_id for z in zones], ["LIVE"])
        # gone for good, not just filtered
        zones = self.store.live("XAUUSD", lambda _id: True)
        self.assertEqual([z.flip_alert_id for z in zones], ["LIVE"])

    def test_zones_are_per_symbol(self):
        self.store.add(dh1())
        self.assertEqual(self.store.live("EURUSD", lambda _id: True), [])

    def test_a_zone_round_trips(self):
        self.store.add(sh1())
        [zone] = self.store.live("xauusd", lambda _id: True)
        self.assertEqual(zone, sh1())


class ZoneLifeTests(unittest.TestCase):
    """A zone is alive exactly as long as its flip candle alert exists."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        path = os.path.join(self.dir, "cracked.db")
        self.candles = alerts.CandleAlertStore(path)
        self.zones = H1ZoneStore(path)

    def tearDown(self):
        self.candles.close()
        self.zones.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_zone_dies_when_its_flip_fires(self):
        flip = self.candles.create(
            111, "XAUUSD", "H1", 4265.0, alerts.CANDLE_BELOW,
            "strike 1 of FLIP to the DOWNSIDE. WATCH!", broadcast=True)
        self.zones.add(dh1(flip=flip.id))
        self.assertEqual(
            len(self.zones.live("XAUUSD", self.candles.exists)), 1)
        self.candles.delete(flip.id)                  # the flip fired
        self.assertEqual(self.zones.live("XAUUSD", self.candles.exists), [])


if __name__ == "__main__":
    unittest.main()
