"""The regime machine: two-strike flips, absorbed opposite zones, the
store, and flip-note parsing (see bias.py)."""

import shutil
import tempfile
import unittest

from crackedalert.bias import (BEARISH, BULLISH, DEMAND, SUPPLY,
                               BiasMachine, BiasStore, BiasState,
                               absorbed_message, flipped_kind_from_note,
                               on_zone_flip, regime_flip_message,
                               seeded_message, strike_message)

NOW = 1_700_000_000
BULL = BiasState(BULLISH, 0, "", NOW)


class OnZoneFlipTests(unittest.TestCase):
    def test_demand_flip_while_bullish_is_strike_one(self):
        out = on_zone_flip(BULL, DEMAND, NOW)
        self.assertEqual(out.kind, "strike")
        self.assertEqual(out.state, BiasState(BULLISH, 1, "strike 1", NOW))
        self.assertEqual(out.flipped_into, SUPPLY)

    def test_second_demand_flip_flips_the_regime(self):
        one = on_zone_flip(BULL, DEMAND, NOW)
        out = on_zone_flip(one.state, DEMAND, NOW + 1)
        self.assertEqual(out.kind, "regime_flip")
        self.assertEqual(out.state.bias, BEARISH)
        self.assertEqual(out.state.strikes, 0)
        # After the regime flips, the flipped demand serves the new
        # bearish regime as supply.
        self.assertEqual(out.flipped_into, SUPPLY)

    def test_supply_flip_while_bullish_is_absorbed(self):
        out = on_zone_flip(BULL, SUPPLY, NOW)
        self.assertEqual(out.kind, "absorbed")
        self.assertEqual(out.state.bias, BULLISH)
        self.assertEqual(out.state.strikes, 0)
        self.assertEqual(out.flipped_into, DEMAND)
        # Absorbing does not clear an existing strike.
        struck = on_zone_flip(BULL, DEMAND, NOW)
        out2 = on_zone_flip(struck.state, SUPPLY, NOW + 1)
        self.assertEqual(out2.kind, "absorbed")
        self.assertEqual(out2.state.strikes, 1)

    def test_mirror_while_bearish(self):
        bear = BiasState(BEARISH, 0, "", NOW)
        out = on_zone_flip(bear, SUPPLY, NOW)          # supply died: strike
        self.assertEqual(out.kind, "strike")
        out2 = on_zone_flip(out.state, SUPPLY, NOW + 1)
        self.assertEqual(out2.kind, "regime_flip")
        self.assertEqual(out2.state.bias, BULLISH)
        out3 = on_zone_flip(bear, DEMAND, NOW)         # demand died: absorbed
        self.assertEqual(out3.kind, "absorbed")
        self.assertEqual(out3.flipped_into, SUPPLY)

    def test_no_bias_is_seeded_by_the_flip(self):
        out = on_zone_flip(None, DEMAND, NOW)
        self.assertEqual(out.kind, "seeded")
        self.assertEqual(out.state.bias, BEARISH)
        self.assertEqual(out.state.strikes, 1)
        out2 = on_zone_flip(None, SUPPLY, NOW)
        self.assertEqual(out2.state.bias, BULLISH)
        self.assertEqual(out2.state.strikes, 1)


class NoteParsingTests(unittest.TestCase):
    def test_flip_notes_map_to_zone_kinds(self):
        self.assertEqual(
            flipped_kind_from_note(
                "strike 1 of FLIP to the DOWNSIDE. WATCH!"), DEMAND)
        self.assertEqual(
            flipped_kind_from_note(
                "strike 1 of FLIP to the UPSIDE. WATCH!"), SUPPLY)

    def test_other_notes_are_ignored(self):
        self.assertIsNone(flipped_kind_from_note("masuk DH1. WATCH!"))
        self.assertIsNone(flipped_kind_from_note(""))
        self.assertIsNone(flipped_kind_from_note("cc guard for pos 5"))


class MessageTests(unittest.TestCase):
    def test_strike_message_names_the_countdown(self):
        out = on_zone_flip(BULL, DEMAND, NOW)
        text = strike_message(out)
        self.assertIn("strike 1 of 2", text)
        self.assertIn("bullish", text)
        self.assertIn("bearish", text)

    def test_regime_message_names_the_new_regime(self):
        one = on_zone_flip(BULL, DEMAND, NOW)
        text = regime_flip_message(on_zone_flip(one.state, DEMAND, NOW))
        self.assertIn("BEARISH", text)

    def test_absorbed_message_says_still_bullish(self):
        text = absorbed_message(on_zone_flip(BULL, SUPPLY, NOW))
        self.assertIn("still", text)
        self.assertIn("bullish", text)
        self.assertIn("acts as demand", text)

    def test_seeded_message_tells_the_operator(self):
        text = seeded_message(on_zone_flip(None, DEMAND, NOW))
        self.assertIn("no bias on record", text)
        self.assertIn("/bias", text)


class BiasStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.machine = BiasMachine(BiasStore(self.dir + "/test.db"))

    def tearDown(self):
        self.machine._store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_unset_then_set_then_flip_roundtrip(self):
        self.assertIsNone(self.machine.status())
        outcome = self.machine.flip(DEMAND)
        self.assertEqual(outcome.kind, "seeded")
        self.assertEqual(self.machine.status().bias, BEARISH)
        self.assertEqual(self.machine.status().strikes, 1)

        state = self.machine.set(BULLISH)
        self.assertEqual(state.strikes, 0)
        self.assertEqual(self.machine.status().bias, BULLISH)

        out = self.machine.flip(DEMAND)
        self.assertEqual(out.kind, "strike")
        # A restart reads the same row.
        machine2 = BiasMachine(BiasStore(self.dir + "/test.db"))
        self.assertEqual(machine2.status().strikes, 1)


if __name__ == "__main__":
    unittest.main()
