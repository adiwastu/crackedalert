"""Zone watches: after a DH1 / S H1 touch, broadcast M5 breaks until the
first CHoCH (see zone_watch.py)."""

import asyncio
import os
import shutil
import tempfile
import unittest

from crackedalert.alerts import CROSSING_DOWN, CROSSING_UP
from crackedalert.wave import BEARISH, BOS, BULLISH, CHOCH, DOJI, WaveEvent
from crackedalert.zone_watch import (ZoneWatch, ZoneWatchStore, _elapsed,
                                     zone_label)

SYMBOL = "XAUUSD"
# Bar timestamps are UTC minutes at the bar's OPEN. 29822940 is
# 2026-09-14 09:00 UTC; the touch lands exactly then.
OPEN = 29822940
TOUCHED_AT = OPEN * 60


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def event(kind, minutes_after_open=0, timeframe="M5", symbol=SYMBOL,
          direction=BEARISH, level=4280.1):
    return WaveEvent(kind=kind, symbol=symbol, timeframe=timeframe,
                     ts=OPEN + minutes_after_open, direction=direction,
                     level=level)


class Sent:
    def __init__(self):
        self.lines = []

    async def __call__(self, text):
        self.lines.append(text)


class ZoneLabelTests(unittest.TestCase):
    def test_demand_is_reached_coming_down(self):
        self.assertEqual(zone_label(CROSSING_DOWN), "DH1")

    def test_supply_is_reached_coming_up(self):
        self.assertEqual(zone_label(CROSSING_UP), "S H1")


class ElapsedTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(_elapsed(0), "0m")
        self.assertEqual(_elapsed(35 * 60), "35m")
        self.assertEqual(_elapsed(3600), "1h")
        self.assertEqual(_elapsed(70 * 60), "1h10m")
        self.assertEqual(_elapsed(65 * 60), "1h05m")


class ZoneWatchTests(unittest.TestCase):
    def setUp(self):
        self.store = ZoneWatchStore(":memory:")
        self.sent = Sent()
        self.watch = ZoneWatch(self.store, self.sent,
                               clock=lambda: TOUCHED_AT)

    def tearDown(self):
        self.store.close()

    def touch(self, label="DH1", level=4282.55):
        run(self.watch.on_touch(SYMBOL, label, level))

    def test_no_watch_means_breaks_are_not_broadcast(self):
        run(self.watch.on_wave_event(event(BOS)))
        self.assertEqual(self.sent.lines, [])

    def test_a_touch_opens_a_watch_and_says_so(self):
        self.touch()
        self.assertEqual(self.watch.watching(SYMBOL),
                         ("DH1", 4282.55, TOUCHED_AT))
        [line] = self.sent.lines
        self.assertIn("DH1 touched on XAUUSD at 4282.55", line)
        self.assertIn("M5 structure until the first CHoCH", line)

    def test_a_bos_during_the_watch_is_broadcast(self):
        self.touch()
        run(self.watch.on_wave_event(event(BOS, minutes_after_open=30)))
        self.assertEqual(
            self.sent.lines[-1],
            "M5 BOS bearish broke 4280.1 at 2026-09-14 09:35 UTC "
            "-- 35m after DH1 touch.")
        self.assertIsNotNone(self.watch.watching(SYMBOL))

    def test_every_bos_is_broadcast_not_just_the_first(self):
        self.touch()
        for minutes in (0, 5, 10):
            run(self.watch.on_wave_event(event(BOS, minutes)))
        self.assertEqual(len(self.sent.lines), 1 + 3)

    def test_the_first_choch_is_broadcast_and_ends_the_watch(self):
        self.touch()
        run(self.watch.on_wave_event(
            event(CHOCH, minutes_after_open=65, direction=BULLISH)))
        self.assertEqual(
            self.sent.lines[-1],
            "M5 CHoCH bullish broke 4280.1 at 2026-09-14 10:10 UTC "
            "-- 1h10m after DH1 touch. Watch ended.")
        self.assertIsNone(self.watch.watching(SYMBOL))

    def test_breaks_after_the_choch_are_not_broadcast(self):
        self.touch()
        run(self.watch.on_wave_event(event(CHOCH, 5)))
        before = len(self.sent.lines)
        run(self.watch.on_wave_event(event(BOS, 10)))
        run(self.watch.on_wave_event(event(CHOCH, 15)))
        self.assertEqual(len(self.sent.lines), before)

    def test_other_timeframes_are_ignored(self):
        self.touch()
        run(self.watch.on_wave_event(event(CHOCH, timeframe="H1")))
        self.assertEqual(len(self.sent.lines), 1)       # just the touch
        self.assertIsNotNone(self.watch.watching(SYMBOL))

    def test_other_symbols_are_ignored(self):
        self.touch()
        run(self.watch.on_wave_event(event(CHOCH, symbol="EURUSD")))
        self.assertEqual(len(self.sent.lines), 1)
        self.assertIsNotNone(self.watch.watching(SYMBOL))

    def test_dojis_are_ignored(self):
        self.touch()
        run(self.watch.on_wave_event(
            WaveEvent(kind=DOJI, symbol=SYMBOL, timeframe="M5", ts=OPEN)))
        self.assertEqual(len(self.sent.lines), 1)

    def test_a_new_touch_restarts_the_watch(self):
        self.touch("DH1", 4282.55)
        later = TOUCHED_AT + 3600
        self.watch._clock = lambda: later
        self.touch("S H1", 4300.0)
        self.assertEqual(self.watch.watching(SYMBOL),
                         ("S H1", 4300.0, later))
        # measured from the newer touch: bar closes 10:10, touch 10:00
        run(self.watch.on_wave_event(event(BOS, minutes_after_open=65)))
        self.assertIn("10m after S H1 touch", self.sent.lines[-1])

    def test_the_utc_offset_applies_to_break_times(self):
        watch = ZoneWatch(self.store, self.sent, utc_offset=7,
                          clock=lambda: TOUCHED_AT)
        run(watch.on_touch(SYMBOL, "DH1", 4282.55))
        run(watch.on_wave_event(event(BOS, minutes_after_open=30)))
        self.assertIn("at 2026-09-14 16:35 UTC+7", self.sent.lines[-1])

    def test_a_failed_broadcast_still_ends_the_watch(self):
        async def broken(text):
            raise RuntimeError("telegram down")

        watch = ZoneWatch(self.store, broken, clock=lambda: TOUCHED_AT)
        run(watch.on_touch(SYMBOL, "DH1", 4282.55))
        run(watch.on_wave_event(event(CHOCH)))
        self.assertIsNone(watch.watching(SYMBOL))
        self.assertEqual(self.store.load(), {})


class ZoneWatchRestartTests(unittest.TestCase):
    """A deploy in the middle of a watch must not silently end it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "zone.db")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_an_open_watch_survives_a_restart(self):
        store = ZoneWatchStore(self.path)
        run(ZoneWatch(store, Sent(), clock=lambda: TOUCHED_AT)
            .on_touch(SYMBOL, "DH1", 4282.55))
        store.close()

        store = ZoneWatchStore(self.path)
        sent = Sent()
        watch = ZoneWatch(store, sent)
        self.assertEqual(watch.watching(SYMBOL),
                         ("DH1", 4282.55, TOUCHED_AT))
        run(watch.on_wave_event(event(CHOCH, 5)))
        self.assertIn("Watch ended.", sent.lines[-1])
        store.close()

        store = ZoneWatchStore(self.path)
        self.assertEqual(store.load(), {})
        store.close()


if __name__ == "__main__":
    unittest.main()
