"""The regime machine: two-strike flips, absorbed opposite zones, the
store, and flip-note parsing (see bias.py)."""

import asyncio
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

    def test_second_demand_flip_flips_the_regime(self):
        one = on_zone_flip(BULL, DEMAND, NOW)
        out = on_zone_flip(one.state, DEMAND, NOW + 1)
        self.assertEqual(out.kind, "regime_flip")
        self.assertEqual(out.state.bias, BEARISH)
        self.assertEqual(out.state.strikes, 0)

    def test_supply_flip_while_bullish_is_absorbed_not_transformed(self):
        out = on_zone_flip(BULL, SUPPLY, NOW)
        self.assertEqual(out.kind, "absorbed")
        self.assertEqual(out.state.bias, BULLISH)
        self.assertEqual(out.state.strikes, 0)
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

    def test_absorbed_message_waits_for_a_proper_zone(self):
        text = absorbed_message(on_zone_flip(BULL, SUPPLY, NOW))
        self.assertIn("still", text)
        self.assertIn("bullish", text)
        self.assertIn("proper demand", text)
        self.assertNotIn("acts as", text)

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


class FlipHookIntegrationTests(unittest.TestCase):
    """The full path that missed a real flip in production: a candle
    alert with a flip note fires through the engine and must reach the
    machine. Guards the field name too (the alert's text is `message`,
    not `note`)."""

    def test_a_firing_flip_alert_counts_through_the_engine(self):
        import asyncio
        import tempfile
        from crackedalert.alerts import (CANDLE_BELOW, CandleAlertEngine,
                                         CandleAlertStore)
        from crackedalert.wave import Bar

        dirpath = tempfile.mkdtemp()
        try:
            candle_store = CandleAlertStore(dirpath + "/candles.db")
            machine = BiasMachine(BiasStore(dirpath + "/bias.db"))
            machine.set(BULLISH)

            async def on_fired(alert):
                flipped = flipped_kind_from_note(alert.message or "")
                if flipped is not None:
                    machine.flip(flipped)

            async def noop(*args, **kwargs):
                pass

            engine = CandleAlertEngine(candle_store, noop,
                                       lambda alert: "fired",
                                       on_fired=on_fired)
            candle_store.create(1, "XAUUSD", "H1", 100.0, CANDLE_BELOW,
                                "strike 1 of FLIP to the DOWNSIDE. WATCH!",
                                broadcast=True)
            candle_store.create(1, "XAUUSD", "H1", 200.0, CANDLE_BELOW,
                                "masuk DH1. WATCH!", broadcast=True)
            # Close below 100 fires the flip alert; 200 is untouched.
            run(engine.on_closed_bar(
                "XAUUSD", "H1",
                Bar(ts=1000, open=110, high=111, low=95, close=99)))
            self.assertEqual(machine.status().bias, BULLISH)
            self.assertEqual(machine.status().strikes, 1)
            # The hourly watcher arms a fresh flip alert with every new
            # imbalance, so the next zone carries the next strike.
            candle_store.create(1, "XAUUSD", "H1", 95.0, CANDLE_BELOW,
                                "strike 1 of FLIP to the DOWNSIDE. WATCH!",
                                broadcast=True)
            # And the second one flips the regime.
            run(engine.on_closed_bar(
                "XAUUSD", "H1",
                Bar(ts=1060, open=98, high=99, low=90, close=92)))
            self.assertEqual(machine.status().bias, BEARISH)
            self.assertEqual(machine.status().strikes, 0)
        finally:
            shutil.rmtree(dirpath, ignore_errors=True)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


if __name__ == "__main__":
    unittest.main()
