"""Market structure (BOS / CHoCH) engine tests -- see wave.py and
docs/wave-spec.md.

Candles are plain integer `open high low close` quadruples, written in
the same order the spec writes them, so the shapes stay readable.
Timestamps are generated at a fixed H1 spacing; the engine only cares
that they advance.
"""

import random
import unittest

from crackedalert.wave import (BEARISH, BOS, BULLISH, CHOCH, DOJI, Bar,
                               apply_bar, new_state, replay)

SYMBOL = "XAUUSD"
TIMEFRAME = "H1"
TS0 = 28000000          # arbitrary UTC-minutes origin
PERIOD = 60             # H1


def series(*rows):
    """Consecutive H1 bars from (open, high, low, close) quadruples."""
    return [Bar(TS0 + i * PERIOD, o, h, l, c)
            for i, (o, h, l, c) in enumerate(rows)]


def run(*rows):
    """Replay a fresh engine over the rows. Returns (state, events)."""
    return replay(series(*rows), new_state(SYMBOL, TIMEFRAME))


def breaks(events):
    """Just the structure breaks, dropping dojis."""
    return [e for e in events if e.kind in (BOS, CHOCH)]


# The full run from the spec: W1-W9, then T, then W10-W11.
W_RUN = (
    (3, 8, 2, 7),       # W1  seeds both moves
    (7, 7, 1, 3),       # W2  down move, validates 8
    (3, 6, 2, 5),       # W3  nothing
    (5, 12, 4, 11),     # W4  BOS #1, direction bullish
    (11, 13, 10, 12),   # W5  extends
    (12, 12, 8, 9),     # W6  down move, validates 13
    (9, 11, 7, 10),     # W7  nothing, but deepens the window to 7
    (10, 16, 9, 15),    # W8  BOS #2
    (15, 16, 5, 6),     # W9  CHoCH, direction flips bearish
    (9, 10, 7, 8),      # T   ignored entirely
    (6, 7, 2, 3),       # W10 extends the down move
    (3, 18, 3, 15),     # W11 widens the high and arms the low
)


def upto(n):
    """Replay the first n bars of the W-run.

    Returns the state after bar n and the events bar n itself produced,
    not the whole run's, so each step can assert what that one bar did.
    """
    state = new_state(SYMBOL, TIMEFRAME)
    bars = series(*W_RUN[:n])
    for bar in bars[:-1]:
        state, _ = apply_bar(state, bar)
    return apply_bar(state, bars[-1])


class SpecMinimalBosTests(unittest.TestCase):
    """Spec: minimal BOS."""

    def test_down_move_validates_then_close_breaks_it(self):
        state, events = run(
            (2, 6, 1, 4),
            (4, 5, 0, 2),
            (2, 9, 1, 7),
        )
        [ev] = breaks(events)
        self.assertEqual(ev.kind, BOS)
        self.assertEqual(ev.direction, BULLISH)
        self.assertEqual(ev.level, 6)
        self.assertEqual(state.direction, BULLISH)

    def test_valid_low_is_the_deepest_of_the_window(self):
        state, _ = run(
            (2, 6, 1, 4),
            (4, 5, 0, 2),
            (2, 9, 1, 7),
        )
        self.assertEqual(state.valid_low, 0)


class SpecExtensionTests(unittest.TestCase):
    """Spec: what does and does not extend an up move."""

    def test_bullish_candle_dipping_below_previous_low_still_extends(self):
        # Candle 3 dips to 1, under candle 2's low of 2, but closes
        # bullish and beats the top, so it extends. Line becomes 1.
        state, events = run(
            (1, 4, 0, 3),
            (3, 6, 2, 5),
            (5, 9, 1, 8),
        )
        self.assertEqual(breaks(events), [])
        self.assertEqual(state.up_top, 9)
        self.assertEqual(state.up_line, 1)

    def test_bearish_candle_above_the_line_is_ignored(self):
        # Candle 4 closes bearish but its low of 6 is above the line of
        # 1, so it is nothing. Candle 5 extends. Top 11, line 5.
        state, events = run(
            (1, 4, 0, 3),
            (3, 6, 2, 5),
            (5, 9, 1, 8),
            (8, 9, 6, 7),
            (7, 11, 5, 10),
        )
        self.assertEqual(breaks(events), [])
        self.assertEqual(state.up_top, 11)
        self.assertEqual(state.up_line, 5)

    def test_bullish_candle_failing_to_beat_the_top_is_ignored(self):
        # Candle 3 closes bullish but its high of 23 does not beat 25,
        # so it does not extend and the line stays at 11.
        state, events = run(
            (13, 25, 11, 22),
            (22, 22, 18, 19),
            (19, 23, 17, 22),
        )
        self.assertEqual(breaks(events), [])
        self.assertEqual(state.up_top, 25)
        self.assertEqual(state.up_line, 11)


class SpecWickRaiseTests(unittest.TestCase):
    """Spec: a wick raises the armed level while waiting."""

    ROWS = (
        (7, 11, 5, 10),
        (10, 10, 4, 5),     # validates 11, window opens at 4
        (5, 7, 3, 6),       # window deepens to 3
        (8, 13, 4, 5),      # bearish, but wicks to 13: level becomes 13
        (13, 23, 12, 22),   # closes 22, above 13
    )

    def test_wick_raises_the_level_and_the_close_breaks_the_raised_one(self):
        state, events = run(*self.ROWS)
        [ev] = breaks(events)
        self.assertEqual(ev.kind, BOS)
        self.assertEqual(ev.level, 13)
        self.assertEqual(state.direction, BULLISH)

    def test_valid_low_comes_from_the_deepest_point_of_the_window(self):
        state, _ = run(*self.ROWS)
        self.assertEqual(state.valid_low, 3)

    def test_close_above_the_old_level_but_below_the_raised_one(self):
        # Closes 12: above the original 11, below the raised 13.
        state, events = run(*self.ROWS[:4], (11, 13, 11, 12))
        self.assertEqual(breaks(events), [])
        self.assertEqual(state.valid_high, 13)
        self.assertIsNone(state.direction)

    def test_breaking_candle_low_wins_when_it_is_the_deepest(self):
        state, _ = run(
            (3, 8, 2, 7),
            (7, 7, 1, 3),       # window opens at 1
            (5, 12, 0, 11),     # breaks 8 and wicks to 0
        )
        self.assertEqual(state.valid_low, 0)


class FullRunTests(unittest.TestCase):
    """Spec: the W-run, asserted at every labelled step."""

    def test_w1_seeds_both_moves_and_arms_nothing(self):
        state, events = upto(1)
        self.assertEqual(events, ())
        self.assertEqual((state.up_top, state.up_line), (8, 2))
        self.assertEqual((state.down_bottom, state.down_line), (2, 8))
        self.assertIsNone(state.valid_high)
        self.assertIsNone(state.valid_low)
        self.assertIsNone(state.direction)

    def test_w2_validates_the_high_and_kills_the_up_move(self):
        state, events = upto(2)
        self.assertEqual(events, ())
        self.assertEqual(state.valid_high, 8)
        self.assertIsNone(state.up_top)
        self.assertEqual((state.down_bottom, state.down_line), (1, 7))
        self.assertEqual(state.window_low, 1)

    def test_w3_does_nothing(self):
        state, events = upto(3)
        self.assertEqual(events, ())
        self.assertEqual(state.valid_high, 8)
        self.assertEqual((state.down_bottom, state.down_line), (1, 7))
        self.assertEqual(state.window_low, 1)

    def test_w4_is_the_first_bos(self):
        state, events = upto(4)
        [ev] = breaks(events)
        self.assertEqual(ev.kind, BOS)
        self.assertEqual(ev.direction, BULLISH)
        self.assertEqual(ev.level, 8)
        self.assertEqual(state.direction, BULLISH)

    def test_w4_commits_the_valid_low_and_disarms_the_high(self):
        state, _ = upto(4)
        self.assertEqual(state.valid_low, 1)
        self.assertIsNone(state.valid_high)

    def test_w4_starts_a_fresh_up_move_and_kills_the_down_move(self):
        state, _ = upto(4)
        self.assertEqual((state.up_top, state.up_line), (12, 4))
        self.assertIsNone(state.down_bottom)

    def test_w5_extends_the_fresh_move(self):
        state, events = upto(5)
        self.assertEqual(breaks(events), [])
        self.assertEqual((state.up_top, state.up_line), (13, 10))

    def test_w6_validates_13_and_opens_a_new_window(self):
        state, events = upto(6)
        self.assertEqual(breaks(events), [])
        self.assertEqual(state.valid_high, 13)
        self.assertEqual(state.window_low, 8)
        self.assertEqual((state.down_bottom, state.down_line), (8, 12))
        self.assertIsNone(state.up_top)

    def test_w7_only_deepens_the_window(self):
        state, events = upto(7)
        self.assertEqual(breaks(events), [])
        self.assertEqual(state.window_low, 7)
        self.assertEqual(state.valid_low, 1)      # untouched: 7 is not below 1
        self.assertEqual(state.valid_high, 13)

    def test_w8_is_a_second_bos_in_the_same_direction(self):
        state, events = upto(8)
        [ev] = breaks(events)
        self.assertEqual(ev.kind, BOS)
        self.assertEqual(ev.direction, BULLISH)
        self.assertEqual(state.valid_low, 7)
        self.assertEqual((state.up_top, state.up_line), (16, 9))

    def test_w9_is_a_choch_and_flips_the_direction(self):
        state, events = upto(9)
        [ev] = breaks(events)
        self.assertEqual(ev.kind, CHOCH)
        self.assertEqual(ev.direction, BEARISH)
        self.assertEqual(ev.level, 7)
        self.assertEqual(state.direction, BEARISH)

    def test_w9_commits_the_high_disarms_the_low_and_reseeds_down(self):
        state, _ = upto(9)
        self.assertEqual(state.valid_high, 16)
        self.assertIsNone(state.valid_low)
        self.assertEqual((state.down_bottom, state.down_line), (5, 16))
        self.assertIsNone(state.up_top)

    def test_t_is_ignored_entirely(self):
        # Bearish, so it cannot be an up move, and its low of 7 does not
        # beat the bottom of 5, so it does not extend the down move.
        before, _ = upto(9)
        after, after_events = upto(10)
        self.assertEqual(after_events, ())
        self.assertEqual(after.valid_high, before.valid_high)
        self.assertIsNone(after.valid_low)
        self.assertEqual((after.down_bottom, after.down_line),
                         (before.down_bottom, before.down_line))
        self.assertIsNone(after.up_top)
        self.assertEqual(after.window_low, before.window_low)

    def test_w10_extends_the_down_move(self):
        state, events = upto(11)
        self.assertEqual(events, ())
        self.assertEqual((state.down_bottom, state.down_line), (2, 7))

    def test_w11_does_not_break_because_it_closes_under_the_armed_high(self):
        _, events = upto(12)
        self.assertEqual(events, ())

    def test_w11_widens_the_high_and_arms_the_low_on_one_bar(self):
        state, _ = upto(12)
        self.assertEqual(state.valid_high, 18)
        self.assertEqual(state.valid_low, 2)

    def test_w11_starts_an_up_move_and_kills_the_down_move(self):
        state, _ = upto(12)
        self.assertEqual((state.up_top, state.up_line), (18, 3))
        self.assertIsNone(state.down_bottom)

    def test_after_w11_both_sides_are_armed_with_direction_bearish(self):
        state, _ = upto(12)
        self.assertEqual(state.direction, BEARISH)
        self.assertIsNotNone(state.valid_high)
        self.assertIsNotNone(state.valid_low)


class TieTests(unittest.TestCase):
    """All comparisons are strict."""

    def test_close_exactly_equal_to_the_valid_high_is_not_a_break(self):
        state, events = run(
            (3, 8, 2, 7),
            (7, 7, 1, 3),       # arms the high at 8
            (5, 8, 4, 8),       # closes exactly 8
        )
        self.assertEqual(breaks(events), [])
        self.assertEqual(state.valid_high, 8)
        self.assertIsNone(state.direction)

    def test_close_exactly_equal_to_the_valid_low_is_not_a_break(self):
        state, events = run(
            (7, 10, 4, 9),
            (9, 12, 8, 11),     # arms the low at 4
            (10, 11, 3, 4),     # closes exactly 4
        )
        self.assertEqual(breaks(events), [])
        self.assertIsNone(state.direction)

    def test_low_exactly_equal_to_the_line_is_not_a_down_move(self):
        state, _ = run(
            (3, 8, 2, 7),       # line 2
            (7, 7, 2, 3),       # low exactly 2
        )
        self.assertIsNone(state.valid_high)
        self.assertEqual((state.up_top, state.up_line), (8, 2))

    def test_high_exactly_equal_to_the_top_does_not_extend(self):
        state, _ = run(
            (3, 8, 2, 7),       # top 8, line 2
            (4, 8, 5, 7),       # high exactly 8
        )
        self.assertEqual((state.up_top, state.up_line), (8, 2))


class DojiTests(unittest.TestCase):
    """open == close: no direction, but still price."""

    ARMED = ((3, 8, 2, 7), (7, 7, 1, 3))      # arms the high at 8

    def test_doji_emits_an_event_carrying_symbol_timeframe_and_ts(self):
        _, events = run((5, 9, 1, 5))
        [ev] = events
        self.assertEqual(ev.kind, DOJI)
        self.assertEqual(ev.symbol, SYMBOL)
        self.assertEqual(ev.timeframe, TIMEFRAME)
        self.assertEqual(ev.ts, TS0)

    def test_doji_cannot_break_even_closing_past_the_armed_level(self):
        state, events = run(*self.ARMED, (9, 10, 8, 9))
        self.assertEqual(breaks(events), [])
        self.assertIsNone(state.direction)

    def test_doji_wick_still_widens_an_armed_level(self):
        state, _ = run(*self.ARMED, (9, 10, 8, 9))
        self.assertEqual(state.valid_high, 10)

    def test_doji_low_still_counts_toward_the_window(self):
        # The doji prints the window's deepest low, so the valid low
        # committed at the break must come from it.
        state, events = run(
            *self.ARMED,
            (4, 5, 0, 4),       # doji, low 0
            (5, 12, 4, 11),     # breaks 8
        )
        self.assertEqual(breaks(events)[-1].kind, BOS)
        self.assertEqual(state.valid_low, 0)

    def test_doji_cannot_validate(self):
        # Bearish-shaped range under the line, but open == close.
        state, _ = run(
            (3, 8, 2, 7),
            (3, 7, 1, 3),       # doji, low 1 is under the line of 2
        )
        self.assertIsNone(state.valid_high)


class ColdStartTests(unittest.TestCase):
    """No direction, nothing armed, possibly for a long time."""

    def test_fresh_state_is_empty(self):
        state = new_state(SYMBOL, TIMEFRAME)
        self.assertIsNone(state.direction)
        self.assertIsNone(state.valid_high)
        self.assertIsNone(state.valid_low)
        self.assertEqual(state.symbol, SYMBOL)
        self.assertEqual(state.timeframe, TIMEFRAME)

    def test_first_candle_arms_nothing(self):
        state, events = run((3, 8, 2, 7))
        self.assertEqual(events, ())
        self.assertIsNone(state.valid_high)
        self.assertIsNone(state.valid_low)
        self.assertIsNone(state.direction)

    def test_first_bullish_candle_seeds_both_moves(self):
        state, _ = run((3, 8, 2, 7))
        self.assertEqual((state.up_top, state.up_line), (8, 2))
        self.assertEqual((state.down_bottom, state.down_line), (2, 8))

    def test_first_bearish_candle_seeds_both_moves_the_same_way(self):
        state, _ = run((7, 8, 2, 3))
        self.assertEqual((state.up_top, state.up_line), (8, 2))
        self.assertEqual((state.down_bottom, state.down_line), (2, 8))

    def test_engine_can_watch_indefinitely_without_arming(self):
        # Nothing beats the first candle's range either way.
        state, events = run(
            (3, 10, 1, 7),
            (5, 9, 2, 6),
            (6, 8, 3, 7),
            (5, 7, 4, 6),
        )
        self.assertEqual(events, ())
        self.assertIsNone(state.direction)
        self.assertIsNone(state.valid_high)
        self.assertIsNone(state.valid_low)

    def test_first_break_is_a_bos_whichever_side_it_comes_on(self):
        # The down tracker is the only path to a bearish first break.
        state, events = run(
            (7, 10, 4, 9),
            (9, 12, 8, 11),     # up move: arms the low at 4
            (10, 11, 2, 3),     # closes 3, under 4
        )
        [ev] = breaks(events)
        self.assertEqual(ev.kind, BOS)
        self.assertEqual(ev.direction, BEARISH)
        self.assertEqual(state.direction, BEARISH)


class SeedingTests(unittest.TestCase):
    """Seeding takes a high and a low and nothing else."""

    SHAPE = staticmethod(
        lambda s: (s.up_top, s.up_line, s.down_bottom, s.down_line))

    def test_a_doji_first_bar_seeds_both_moves(self):
        state, events = run((5, 9, 1, 5))
        self.assertEqual((state.up_top, state.up_line), (9, 1))
        self.assertEqual((state.down_bottom, state.down_line), (1, 9))
        [ev] = events
        self.assertEqual(ev.kind, DOJI)

    def test_seeding_ignores_direction_entirely(self):
        # Same high and low, three different candle directions: the
        # seeded shape must be identical.
        doji, _ = run((5, 9, 1, 5))
        bullish, _ = run((2, 9, 1, 8))
        bearish, _ = run((8, 9, 1, 2))
        self.assertEqual(self.SHAPE(doji), self.SHAPE(bullish))
        self.assertEqual(self.SHAPE(doji), self.SHAPE(bearish))

    def test_a_window_opening_on_a_doji_matches_one_opening_on_a_body(self):
        # The deciding argument: a scan's result must not depend on
        # where the window was sliced. A leading doji and a leading
        # directional bar of the same range must leave the engine in
        # the same place once the rest of the run has been folded in.
        tail = W_RUN[1:]
        via_doji, doji_events = run((5, 8, 2, 5), *tail)
        via_body, body_events = run((3, 8, 2, 7), *tail)
        self.assertEqual(self.SHAPE(via_doji), self.SHAPE(via_body))
        self.assertEqual(via_doji.direction, via_body.direction)
        self.assertEqual(via_doji.valid_high, via_body.valid_high)
        self.assertEqual(via_doji.valid_low, via_body.valid_low)
        self.assertEqual(breaks(doji_events), breaks(body_events))

    def test_seeding_does_not_revive_a_move_killed_by_a_break(self):
        # W4's BOS kills the down move. W5 extends the live up move; it
        # must not seed a replacement down move into the empty slot.
        state, _ = upto(5)
        self.assertEqual((state.up_top, state.up_line), (13, 10))
        self.assertIsNone(state.down_bottom)
        self.assertIsNone(state.down_line)

    def test_seeding_does_not_revive_a_move_displaced_by_its_opposite(self):
        # W2's down move kills the up move. W3 does nothing at all; it
        # must not seed a replacement up move.
        state, _ = upto(3)
        self.assertEqual((state.down_bottom, state.down_line), (1, 7))
        self.assertIsNone(state.up_top)
        self.assertIsNone(state.up_line)


class ArmedLevelTests(unittest.TestCase):
    """The armed level is the furthest price went: the move's extreme or
    the validating candle's own wick, whichever is further out."""

    def test_bullish_arms_the_top_when_the_top_is_the_outer_value(self):
        # The up move's top of 8 is above the validating candle's own
        # high of 7, so the top is the level.
        state, _ = run(
            (3, 8, 2, 7),
            (7, 7, 1, 3),
        )
        self.assertEqual(state.valid_high, 8)

    def test_bullish_arms_the_wick_when_the_validating_candle_is_outer(self):
        # The validating candle closed against the move, so its high of
        # 15 was never folded into the top of 10. The wick wins.
        state, _ = run(
            (7, 10, 6, 9),
            (9, 15, 5, 7),
        )
        self.assertEqual(state.valid_high, 15)

    def test_bearish_arms_the_bottom_when_the_bottom_is_the_outer_value(self):
        # Mirror: the down move's bottom of 2 is below the validating
        # candle's own low of 3.
        state, _ = run(
            (9, 10, 2, 3),
            (4, 12, 3, 11),
        )
        self.assertEqual(state.valid_low, 2)

    def test_bearish_arms_the_wick_when_the_validating_candle_is_outer(self):
        state, _ = run(
            (9, 10, 6, 7),
            (5, 15, 3, 14),
        )
        self.assertEqual(state.valid_low, 3)


class OutwardOnlyTests(unittest.TestCase):
    """Levels only move outward."""

    def test_a_lower_validated_high_does_not_replace_a_higher_one(self):
        state, events = run(
            (7, 11, 5, 10),
            (10, 10, 4, 5),     # validates 11
            (5, 7, 3, 6),
            (8, 13, 4, 5),      # wick raises the level to 13
            (5, 12, 4, 11),     # up move: top 12, under the armed 13
            (11, 12, 3, 4),     # down move validates 12 -- must not win
        )
        self.assertEqual(breaks(events), [])
        self.assertEqual(state.valid_high, 13)

    def test_a_candle_lowers_the_valid_low_and_validates_a_high_at_once(self):
        state, _ = upto(8)
        self.assertEqual(state.valid_low, 7)
        self.assertIsNone(state.valid_high)
        state, events = apply_bar(
            state, Bar(TS0 + 8 * PERIOD, 15, 16, 6, 10))
        self.assertEqual(events, ())
        self.assertEqual(state.valid_low, 6)      # wick lowered it
        self.assertEqual(state.valid_high, 16)    # down move validated it


class ReplayTests(unittest.TestCase):
    """Deterministic and replayable."""

    def test_same_sequence_gives_the_same_state(self):
        a, ea = run(*W_RUN)
        b, eb = run(*W_RUN)
        self.assertEqual(a, b)
        self.assertEqual(ea, eb)

    def test_one_bar_at_a_time_matches_bulk_replay(self):
        bulk_state, bulk_events = run(*W_RUN)
        state = new_state(SYMBOL, TIMEFRAME)
        events = []
        for b in series(*W_RUN):
            state, evs = apply_bar(state, b)
            events.extend(evs)
        self.assertEqual(state, bulk_state)
        self.assertEqual(tuple(events), bulk_events)

    def test_apply_bar_does_not_mutate_the_input_state(self):
        state = new_state(SYMBOL, TIMEFRAME)
        before = state
        apply_bar(state, Bar(TS0, 3, 8, 2, 7))
        self.assertEqual(state, before)

    def test_last_ts_tracks_the_newest_bar(self):
        state, _ = run(*W_RUN)
        self.assertEqual(state.last_ts, TS0 + (len(W_RUN) - 1) * PERIOD)


class InvariantTests(unittest.TestCase):
    """Properties that must hold after every bar, on any input."""

    @staticmethod
    def _random_bars(rng, count):
        for i in range(count):
            o, c = rng.randint(1, 40), rng.randint(1, 40)
            yield Bar(TS0 + i * PERIOD, o,
                      max(o, c) + rng.randint(0, 6),
                      min(o, c) - rng.randint(0, 6), c)

    def test_armed_levels_and_windows_stay_paired(self):
        # A window is open exactly when its opposite level is armed.
        # Arming, widening, committing and disarming must all preserve
        # this or a break would commit a level from a stale window.
        rng = random.Random(7)
        for _ in range(300):
            state = new_state(SYMBOL, TIMEFRAME)
            for bar in self._random_bars(rng, 60):
                state, _ = apply_bar(state, bar)
                self.assertEqual(state.valid_high is None,
                                 state.window_low is None)
                self.assertEqual(state.valid_low is None,
                                 state.window_high is None)

    def test_a_move_is_always_live_after_the_first_bar(self):
        # Seeding can only happen when there is no move at all, so if
        # both slots ever emptied, a later bar would silently reseed.
        rng = random.Random(11)
        for _ in range(300):
            state = new_state(SYMBOL, TIMEFRAME)
            for bar in self._random_bars(rng, 60):
                state, _ = apply_bar(state, bar)
                self.assertFalse(state.up_top is None
                                 and state.down_bottom is None)


if __name__ == "__main__":
    unittest.main()
