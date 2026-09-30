"""M15 imbalances on an H1 imbalance's own move (see nested_fvg.py).

Bars are raw trendbar dicts built from real prices, like the gateway
sends them, so nested_entries is exercised on exactly what main.py
hands it.
"""

import unittest

from crackedalert import alerts
from crackedalert.fvg import fresh_imbalance
from crackedalert.nested_fvg import nested_entries

SCALE = 100000


def bar(ts, low, high):
    return {"utcTimestampInMinutes": ts, "low": round(low * SCALE),
            "deltaHigh": round((high - low) * SCALE)}


def gaps(bars, period=15):
    """(newest_ts, direction) of every FVG in a run of bars. Guards the
    fixtures: each case below depends on exactly which gaps exist."""
    found = []
    for i in range(2, len(bars)):
        which = fresh_imbalance(bars[i - 2:i + 1], period)
        if which:
            found.append((bars[i]["utcTimestampInMinutes"], which))
    return found


# Bullish H1 gap (4270, 4282) built by three H1 candles opening at 600,
# 660 and 720: the move spans M15 candles opening 600 .. 765.
H1_BULLISH = [bar(600, 4262, 4270), bar(660, 4268, 4290),
              bar(720, 4282, 4300)]

# A taller bullish H1 gap (4270, 4300), for two M15 gaps to sit in.
H1_WIDE = [bar(600, 4262, 4270), bar(660, 4268, 4310),
           bar(720, 4300, 4320)]

# Bearish mirror: gap (4300, 4312).
H1_BEARISH = [bar(600, 4312, 4320), bar(660, 4290, 4310),
              bar(720, 4280, 4300)]


def flat(ts_list, low, high):
    return [bar(ts, low, high) for ts in ts_list]


# Twelve M15 candles for the bullish move: one gap (4271, 4273), fully
# inside the H1 gap, completing on 630, then candles that overlap so no
# further gap forms.
BULLISH_MOVE = ([bar(600, 4262, 4271), bar(615, 4268, 4276),
                 bar(630, 4273, 4284), bar(645, 4274, 4284)]
                + flat(range(660, 780, 15), 4276, 4286))

# The same gap shape printed later, after the move, when price has come
# back to the zone. It overlaps the H1 gap but is a different move.
LATER_GAP = [bar(900, 4262, 4271), bar(915, 4268, 4276),
             bar(930, 4273, 4284)]


class FixtureTests(unittest.TestCase):
    """Every case below assumes these really are what they claim."""

    def test_the_h1_fixtures_are_h1_imbalances(self):
        self.assertEqual(fresh_imbalance(H1_BULLISH, 60), "bullish")
        self.assertEqual(fresh_imbalance(H1_BEARISH, 60), "bearish")

    def test_the_move_is_twelve_m15_candles_with_one_gap(self):
        self.assertEqual(len(BULLISH_MOVE), 12)
        self.assertEqual([b["utcTimestampInMinutes"] for b in BULLISH_MOVE],
                         list(range(600, 780, 15)))
        self.assertEqual(gaps(BULLISH_MOVE), [(630, "bullish")])

    def test_the_later_gap_is_a_real_one(self):
        self.assertEqual(gaps(LATER_GAP), [(930, "bullish")])


class SameMoveTests(unittest.TestCase):

    def test_a_gap_on_the_move_that_overlaps_the_h1_gap_arms_an_entry(self):
        [entry] = nested_entries(BULLISH_MOVE, H1_BULLISH, "bullish")
        self.assertAlmostEqual(entry.level, 4271.0)      # its candle 1 high
        self.assertAlmostEqual(entry.bottom, 4271.0)
        self.assertAlmostEqual(entry.top, 4273.0)
        self.assertEqual(entry.direction, alerts.CROSSING_DOWN)
        self.assertEqual(entry.note, "masuk DM15. WATCH!")
        self.assertEqual(entry.newest_ts, 630)

    def test_a_later_gap_that_lines_up_is_a_different_move_and_ignored(self):
        # The bug this replaces. It overlaps the H1 gap perfectly, but it
        # printed long after the three H1 candles that made the gap.
        self.assertEqual(
            nested_entries(LATER_GAP, H1_BULLISH, "bullish"), [])

    def test_the_later_gap_is_ignored_even_beside_a_valid_one(self):
        [entry] = nested_entries(BULLISH_MOVE + LATER_GAP, H1_BULLISH,
                                 "bullish")
        self.assertEqual(entry.newest_ts, 630)

    def test_a_gap_on_the_move_that_is_clear_of_the_h1_gap_arms_nothing(self):
        above = ([bar(600, 4290, 4295), bar(615, 4294, 4305),
                  bar(630, 4300, 4310)]
                 + flat(range(645, 780, 15), 4296, 4312))
        self.assertEqual(gaps(above), [(630, "bullish")])
        self.assertEqual(nested_entries(above, H1_BULLISH, "bullish"), [])

    def test_a_gap_pointing_the_other_way_arms_nothing(self):
        # A bearish M15 gap (4275, 4278) sitting entirely inside the
        # bullish H1 gap (4270, 4282). Deliberately inside: a bearish
        # triplet read with bullish bounds comes out inverted and would
        # fail the overlap check by accident, hiding a missing direction
        # check. Fully inside, only the direction check can reject it.
        bearish = ([bar(600, 4278, 4281), bar(615, 4274, 4279),
                    bar(630, 4272, 4275)]
                   + flat(range(645, 780, 15), 4272, 4281))
        self.assertEqual(gaps(bearish), [(630, "bearish")])
        self.assertEqual(nested_entries(bearish, H1_BULLISH, "bullish"), [])

    def test_the_bearish_mirror_arms_an_s_m15_entry(self):
        move = ([bar(600, 4302, 4311), bar(615, 4296, 4304),
                 bar(630, 4292, 4299)]
                + flat(range(645, 780, 15), 4288, 4298))
        self.assertEqual(gaps(move), [(630, "bearish")])
        [entry] = nested_entries(move, H1_BEARISH, "bearish")
        self.assertAlmostEqual(entry.level, 4302.0)      # its candle 1 low
        self.assertEqual(entry.direction, alerts.CROSSING_UP)
        self.assertEqual(entry.note, "masuk S M15. WATCH!")

    def test_no_m15_bars_arms_nothing(self):
        self.assertEqual(nested_entries([], H1_BULLISH, "bullish"), [])


class WindowEdgeTests(unittest.TestCase):
    """The move is the M15 candles opening from the H1 gap's first candle
    up to, not including, three hours later."""

    def test_a_candle_opening_exactly_as_the_move_starts_counts(self):
        # The gap's first candle is the very first candle of the move.
        self.assertEqual(BULLISH_MOVE[0]["utcTimestampInMinutes"], 600)
        self.assertEqual(len(nested_entries(BULLISH_MOVE, H1_BULLISH,
                                            "bullish")), 1)

    def test_a_gap_whose_first_candle_is_before_the_move_does_not_count(self):
        early = [bar(585, 4262, 4271)] + BULLISH_MOVE[1:3] + flat(
            range(645, 780, 15), 4276, 4286)
        early[1] = bar(600, 4268, 4276)
        early[2] = bar(615, 4273, 4284)
        # Gap completes on 615, but its first candle opened at 585.
        self.assertEqual(gaps(early)[0], (615, "bullish"))
        self.assertEqual(nested_entries(early, H1_BULLISH, "bullish"), [])

    def test_a_gap_completing_on_the_last_candle_of_the_move_counts(self):
        # Quiet candles, then a gap whose third candle opens at 765, the
        # last M15 candle of the three hours starting at 600.
        last = flat(range(600, 735, 15), 4276, 4286) + [
            bar(735, 4262, 4271), bar(750, 4268, 4276),
            bar(765, 4273, 4284)]
        self.assertIn((765, "bullish"), gaps(last))
        self.assertEqual(
            [e.newest_ts for e in nested_entries(last, H1_BULLISH, "bullish")],
            [765])

    def test_a_gap_completing_when_the_move_has_ended_is_outside(self):
        # The same gap one candle later: its third candle opens at 780,
        # exactly three hours after 600, so it belongs to the next move.
        after = flat(range(600, 750, 15), 4276, 4286) + [
            bar(750, 4262, 4271), bar(765, 4268, 4276),
            bar(780, 4273, 4284)]
        self.assertIn((780, "bullish"), gaps(after))
        self.assertEqual(nested_entries(after, H1_BULLISH, "bullish"), [])

    def test_a_hole_in_the_candles_breaks_the_triplet(self):
        # Missing 615: 600 and 630 are not adjacent M15 candles.
        holey = [BULLISH_MOVE[0], BULLISH_MOVE[2]] + BULLISH_MOVE[3:]
        self.assertEqual(nested_entries(holey, H1_BULLISH, "bullish"), [])


class BackToBackTests(unittest.TestCase):
    """One impulse prints a gap on candle after candle: alert the first."""

    # Two adjacent gaps, both inside the H1 gap (4270, 4282):
    #   (4271, 4273) completing on 630, then (4275, 4277) completing on 645.
    RUN = ([bar(600, 4262, 4271), bar(615, 4268, 4276),
            bar(630, 4273, 4284), bar(645, 4277, 4288)]
           + flat(range(660, 780, 15), 4279, 4290))

    def test_the_fixture_has_two_adjacent_gaps(self):
        self.assertEqual(gaps(self.RUN),
                         [(630, "bullish"), (645, "bullish")])

    def test_only_the_first_of_a_consecutive_run_alerts(self):
        [entry] = nested_entries(self.RUN, H1_BULLISH, "bullish")
        self.assertEqual(entry.newest_ts, 630)

    def test_a_gap_outside_the_h1_gap_does_not_block_the_one_inside(self):
        # Price wicks just under the H1 gap and bounces. The first gap
        # forms below it and arms nothing; the next lands inside it. It
        # is not a duplicate of something never armed, so it must alert.
        # (Deciding "back to back" among all M15 gaps instead of only the
        # overlapping ones would skip it.)
        bounce = ([bar(600, 4255, 4260), bar(615, 4259, 4270),
                   bar(630, 4263, 4276), bar(645, 4273, 4285)]
                  + flat(range(660, 780, 15), 4276, 4288))
        self.assertEqual(gaps(bounce),
                         [(630, "bullish"), (645, "bullish")])
        [entry] = nested_entries(bounce, H1_BULLISH, "bullish")
        self.assertEqual(entry.newest_ts, 645)
        self.assertAlmostEqual(entry.level, 4270.0)

    def test_gaps_separated_by_quiet_candles_both_alert(self):
        # A taller H1 gap (4270, 4300), with two M15 gaps in it that are
        # not adjacent: (4271, 4273) completing on 630 and (4284, 4286)
        # completing on 675, with candles between that gap nothing.
        apart = ([bar(600, 4262, 4271), bar(615, 4268, 4276),
                  bar(630, 4273, 4284), bar(645, 4274, 4284),
                  bar(660, 4275, 4285), bar(675, 4286, 4296)]
                 + flat(range(690, 780, 15), 4284, 4297))
        self.assertEqual(gaps(apart), [(630, "bullish"), (675, "bullish")])
        self.assertEqual(
            [e.newest_ts for e in nested_entries(apart, H1_WIDE, "bullish")],
            [630, 675])


if __name__ == "__main__":
    unittest.main()
