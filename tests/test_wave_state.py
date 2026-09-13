"""Wave state persistence, staleness and warm-up (see wave_state.py).

Bars are plain integer `open high low close` quadruples, as in
test_wave.py. The fake fetch stands in for the candle feed so the
warm-up logic is exercised without a gateway.
"""

import asyncio
import unittest

from crackedalert.wave import BEARISH, BOS, BULLISH, Bar, new_state
from crackedalert.wave_state import (PERIOD_MINUTES, WaveService,
                                     WaveStateStore, resumable)

SYMBOL = "XAUUSD"
TIMEFRAME = "H1"
TS0 = 28000000
PERIOD = 60


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def series(*rows, ts0=TS0, period=PERIOD):
    return [Bar(ts0 + i * period, o, h, l, c)
            for i, (o, h, l, c) in enumerate(rows)]


# W1-W4 of the spec run: W4 closes above the high W2 validated, so this
# window produces a BOS. Dropping W4 leaves a window that never breaks,
# and tiling either shape keeps that property.
BREAKING = ((3, 8, 2, 7), (7, 7, 1, 3), (3, 6, 2, 5), (5, 12, 4, 11))
QUIET = ((3, 8, 2, 7), (7, 7, 1, 3), (3, 6, 2, 5))

# The newest bar every window ends on. A wider window reaches further
# back from here, which is how the real fetch behaves.
END_TS = TS0 + 5000 * PERIOD


def window(rows, count, end_ts=END_TS):
    """Exactly `count` consecutive bars ending at end_ts, tiling `rows`."""
    tiled = (list(rows) * (count // len(rows) + 1))[:count]
    return series(*tiled, ts0=end_ts - (count - 1) * PERIOD)


class FakeFetch:
    """Serves a bar list per requested window size, recording the sizes."""

    def __init__(self, by_count):
        self.by_count = by_count
        self.calls = []

    async def __call__(self, symbol, timeframe, count):
        self.calls.append(count)
        return list(self.by_count.get(count, []))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = WaveStateStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_missing_state_loads_as_none(self):
        self.assertIsNone(self.store.load(SYMBOL, TIMEFRAME))

    def test_a_cold_state_round_trips(self):
        state = new_state(SYMBOL, TIMEFRAME)
        self.store.save(state)
        self.assertEqual(self.store.load(SYMBOL, TIMEFRAME), state)

    def test_a_fully_populated_state_round_trips(self):
        state = new_state(SYMBOL, TIMEFRAME)
        state, _ = self._advance(state)
        self.store.save(state)
        loaded = self.store.load(SYMBOL, TIMEFRAME)
        self.assertEqual(loaded, state)
        # every field, not just the headline levels
        self.assertIsNotNone(loaded.window_high)
        self.assertIsNotNone(loaded.direction)
        self.assertIsNotNone(loaded.last_ts)

    def test_saving_the_same_key_overwrites(self):
        first = new_state(SYMBOL, TIMEFRAME)
        self.store.save(first)
        second, _ = self._advance(new_state(SYMBOL, TIMEFRAME))
        self.store.save(second)
        self.assertEqual(self.store.load(SYMBOL, TIMEFRAME), second)

    def test_keys_do_not_collide(self):
        h1 = new_state(SYMBOL, "H1")
        m5, _ = self._advance(new_state(SYMBOL, "M5"))
        self.store.save(h1)
        self.store.save(m5)
        self.assertEqual(self.store.load(SYMBOL, "H1"), h1)
        self.assertEqual(self.store.load(SYMBOL, "M5"), m5)

    def test_lookup_is_case_insensitive(self):
        self.store.save(new_state(SYMBOL, TIMEFRAME))
        self.assertIsNotNone(self.store.load("xauusd", "h1"))

    def test_delete_removes_the_row(self):
        self.store.save(new_state(SYMBOL, TIMEFRAME))
        self.store.delete(SYMBOL, TIMEFRAME)
        self.assertIsNone(self.store.load(SYMBOL, TIMEFRAME))

    @staticmethod
    def _advance(state):
        from crackedalert.wave import replay
        return replay(series(*BREAKING), state)


class ResumableTests(unittest.TestCase):
    """Strict: only the immediately following bar resumes."""

    def _stored(self, last_ts, timeframe=TIMEFRAME):
        from dataclasses import replace as dc_replace
        return dc_replace(new_state(SYMBOL, timeframe), last_ts=last_ts)

    def test_the_next_bar_resumes(self):
        self.assertTrue(resumable(self._stored(TS0), TS0 + PERIOD))

    def test_a_one_bar_gap_does_not_resume(self):
        self.assertFalse(resumable(self._stored(TS0), TS0 + 2 * PERIOD))

    def test_the_same_bar_does_not_resume(self):
        self.assertFalse(resumable(self._stored(TS0), TS0))

    def test_an_older_bar_does_not_resume(self):
        self.assertFalse(resumable(self._stored(TS0), TS0 - PERIOD))

    def test_state_with_no_last_ts_does_not_resume(self):
        self.assertFalse(resumable(new_state(SYMBOL, TIMEFRAME), TS0))

    def test_the_period_comes_from_the_timeframe(self):
        self.assertTrue(resumable(self._stored(TS0, "M5"), TS0 + 5))
        self.assertFalse(resumable(self._stored(TS0, "M5"), TS0 + 60))

    def test_an_unknown_timeframe_never_resumes(self):
        # MN1 is not a fixed number of minutes, so it always rebuilds.
        self.assertNotIn("MN1", PERIOD_MINUTES)
        self.assertFalse(resumable(self._stored(TS0, "MN1"), TS0 + 1))


class WarmUpTests(unittest.TestCase):
    def setUp(self):
        self.store = WaveStateStore(":memory:")

    def tearDown(self):
        self.store.close()

    def _service(self, fetch, windows=(100, 300, 1000)):
        return WaveService(self.store, fetch, windows=windows)

    def test_stops_at_the_first_window_that_breaks(self):
        fetch = FakeFetch({100: window(BREAKING, 100)})
        service = self._service(fetch)
        state = run(service._warm_up(SYMBOL, TIMEFRAME))
        self.assertEqual(fetch.calls, [100])        # did not widen
        self.assertEqual(state.direction, BULLISH)

    def test_widens_when_the_first_window_does_not_break(self):
        fetch = FakeFetch({100: window(QUIET, 100),
                           300: window(BREAKING, 300)})
        service = self._service(fetch)
        state = run(service._warm_up(SYMBOL, TIMEFRAME))
        self.assertEqual(fetch.calls, [100, 300])
        self.assertEqual(state.direction, BULLISH)

    def test_widens_all_the_way_to_the_last_window(self):
        fetch = FakeFetch({100: window(QUIET, 100),
                           300: window(QUIET, 300),
                           1000: window(BREAKING, 1000)})
        service = self._service(fetch)
        state = run(service._warm_up(SYMBOL, TIMEFRAME))
        self.assertEqual(fetch.calls, [100, 300, 1000])
        self.assertEqual(state.direction, BULLISH)

    def test_no_break_anywhere_stays_undirected(self):
        fetch = FakeFetch({100: window(QUIET, 100),
                           300: window(QUIET, 300),
                           1000: window(QUIET, 1000)})
        service = self._service(fetch)
        state = run(service._warm_up(SYMBOL, TIMEFRAME))
        self.assertEqual(fetch.calls, [100, 300, 1000])
        self.assertIsNone(state.direction)

    def test_exhausted_history_stops_widening(self):
        # Only 39 bars exist, so asking for 300 would return the same
        # data again. One short window is enough to know that.
        fetch = FakeFetch({100: window(QUIET, 39)})
        service = self._service(fetch)
        state = run(service._warm_up(SYMBOL, TIMEFRAME))
        self.assertEqual(fetch.calls, [100])
        self.assertIsNone(state.direction)

    def test_no_data_at_all_leaves_a_cold_state(self):
        fetch = FakeFetch({})
        service = self._service(fetch)
        state = run(service._warm_up(SYMBOL, TIMEFRAME))
        self.assertEqual(state, new_state(SYMBOL, TIMEFRAME))

    def test_a_wider_window_is_replayed_fresh_not_layered_on_the_narrow_one(self):
        # "Discard the result, raise N, and rerun from the oldest candle
        # of the larger window." The state must equal a clean replay of
        # the 300 window, with nothing carried over from the 100 scan.
        from crackedalert.wave import replay
        narrow, wide = window(QUIET, 100), window(BREAKING, 300)
        service = self._service(FakeFetch({100: narrow, 300: wide}))
        state = run(service._warm_up(SYMBOL, TIMEFRAME))
        expected, _ = replay(wide, new_state(SYMBOL, TIMEFRAME))
        self.assertEqual(state, expected)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.store = WaveStateStore(":memory:")
        self.events = []

        async def on_event(event):
            self.events.append(event)

        self.on_event = on_event

    def tearDown(self):
        self.store.close()

    def _service(self, fetch, **kw):
        return WaveService(self.store, fetch, on_event=self.on_event,
                           windows=(100,), **kw)

    def test_first_bar_with_no_stored_state_warms_up(self):
        fetch = FakeFetch({100: series(*BREAKING)})
        service = self._service(fetch)
        nxt = Bar(TS0 + 4 * PERIOD, 11, 13, 10, 12)
        run(service.on_closed_bar(SYMBOL, TIMEFRAME, nxt))
        self.assertEqual(fetch.calls, [100])
        self.assertEqual(service.state(SYMBOL, TIMEFRAME).direction, BULLISH)

    def test_a_resumable_stored_state_is_used_without_fetching(self):
        from crackedalert.wave import replay
        warmed, _ = replay(series(*BREAKING), new_state(SYMBOL, TIMEFRAME))
        self.store.save(warmed)
        fetch = FakeFetch({100: series(*BREAKING)})
        service = self._service(fetch)
        nxt = Bar(warmed.last_ts + PERIOD, 11, 13, 10, 12)
        run(service.on_closed_bar(SYMBOL, TIMEFRAME, nxt))
        self.assertEqual(fetch.calls, [])           # no warm-up needed
        self.assertEqual(service.state(SYMBOL, TIMEFRAME).last_ts, nxt.ts)

    def test_a_stale_stored_state_is_discarded_and_rebuilt(self):
        from crackedalert.wave import replay
        warmed, _ = replay(series(*BREAKING), new_state(SYMBOL, TIMEFRAME))
        self.store.save(warmed)
        fetch = FakeFetch({100: series(*BREAKING)})
        service = self._service(fetch)
        stale = Bar(warmed.last_ts + 9 * PERIOD, 11, 13, 10, 12)
        run(service.on_closed_bar(SYMBOL, TIMEFRAME, stale))
        self.assertEqual(fetch.calls, [100])        # rebuilt

    def test_every_folded_bar_is_written_back(self):
        fetch = FakeFetch({100: series(*BREAKING)})
        service = self._service(fetch)
        nxt = Bar(TS0 + 4 * PERIOD, 11, 13, 10, 12)
        run(service.on_closed_bar(SYMBOL, TIMEFRAME, nxt))
        self.assertEqual(self.store.load(SYMBOL, TIMEFRAME).last_ts, nxt.ts)

    def test_a_bar_the_warm_up_already_covered_is_not_folded_twice(self):
        bars = series(*BREAKING)
        fetch = FakeFetch({100: bars})
        service = self._service(fetch)
        run(service.on_closed_bar(SYMBOL, TIMEFRAME, bars[-1]))
        state = service.state(SYMBOL, TIMEFRAME)
        expected, _ = self._replayed(bars)
        self.assertEqual(state, expected)

    def test_warm_up_breaks_are_not_emitted(self):
        # The warm-up window contains a BOS, but replaying history only
        # reconstructs where structure stands; it announces nothing.
        bars = series(*BREAKING)
        service = self._service(FakeFetch({100: bars}))
        run(service.on_closed_bar(SYMBOL, TIMEFRAME, bars[-1]))
        self.assertEqual(self.events, [])

    def test_live_breaks_are_emitted(self):
        # Warm up on a quiet window, then feed a bar that breaks.
        quiet = series(*QUIET)
        service = self._service(FakeFetch({100: quiet}))
        breaker = Bar(quiet[-1].ts + PERIOD, 5, 12, 4, 11)
        run(service.on_closed_bar(SYMBOL, TIMEFRAME, breaker))
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0].kind, BOS)
        self.assertEqual(self.events[0].direction, BULLISH)

    def test_state_is_kept_per_key(self):
        fetch = FakeFetch({100: series(*BREAKING)})
        service = self._service(fetch)
        nxt = Bar(TS0 + 4 * PERIOD, 11, 13, 10, 12)
        run(service.on_closed_bar(SYMBOL, TIMEFRAME, nxt))
        run(service.on_closed_bar(SYMBOL, "M5", nxt))
        self.assertIsNotNone(service.state(SYMBOL, TIMEFRAME))
        self.assertIsNotNone(service.state(SYMBOL, "M5"))
        self.assertIsNotNone(self.store.load(SYMBOL, "M5"))

    def test_a_failing_event_handler_does_not_lose_the_bar(self):
        async def broken(event):
            raise RuntimeError("consumer down")

        quiet = series(*QUIET)
        service = WaveService(self.store, FakeFetch({100: quiet}),
                              on_event=broken, windows=(100,))
        breaker = Bar(quiet[-1].ts + PERIOD, 5, 12, 4, 11)
        run(service.on_closed_bar(SYMBOL, TIMEFRAME, breaker))
        self.assertEqual(
            self.store.load(SYMBOL, TIMEFRAME).last_ts, breaker.ts)

    @staticmethod
    def _replayed(bars):
        from crackedalert.wave import replay
        return replay(bars, new_state(SYMBOL, TIMEFRAME))


if __name__ == "__main__":
    unittest.main()
