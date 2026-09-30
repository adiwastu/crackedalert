"""M15 imbalances that belong to an H1 imbalance's own move.

An H1 fair value gap is built by three consecutive H1 candles, which is
twelve M15 candles. The M15 gaps worth acting on are the ones inside
that same run of candles: the impulse printed on both timeframes at
once, and the M15 gap that lines up with the H1 gap is a finer entry
into it.

The window is fixed by the H1 gap, so this needs no memory of past H1
zones, and an M15 gap that happens to line up with an H1 gap much later
is not the same move and is never considered.
"""

from dataclasses import dataclass
from typing import List

from .fvg import (NESTED_ALERT_SPECS, ImbalanceGate, candle_high,
                  candle_low, fresh_imbalance, gap_bounds, overlaps)

H1_MINUTES = 60
M15_MINUTES = 15


@dataclass(frozen=True)
class NestedEntry:
    """An M15 gap inside the H1 move, and the entry alert it arms."""
    bottom: float           # the M15 gap
    top: float
    level: float            # entry alert level: its candle 1
    direction: str          # alert direction
    note: str
    newest_ts: int          # open of the M15 candle that completed it


def nested_entries(m15_bars: List[dict], h1_bars: List[dict],
                   which: str) -> List[NestedEntry]:
    """M15 gaps on the H1 move that overlap the H1 gap.

    `h1_bars` must end with the triplet that completed the H1 imbalance
    and `which` is its direction, as fresh_imbalance returned it.
    `m15_bars` is any run of completed M15 bars, oldest first, that
    covers the move.

    A gap counts when it points the same way as the H1 gap and overlaps
    its range at all: inside it, crossing an edge, or slicing through.

    Consecutive M15 gaps are the same impulse, so only the first of a
    run alerts. That is decided among gaps that overlap the H1 gap, not
    among all M15 gaps: one that forms outside the H1 gap arms nothing,
    so a later one inside it is not a duplicate of it.
    """
    h1_bottom, h1_top = gap_bounds(h1_bars, which)
    start = int(h1_bars[-3].get("utcTimestampInMinutes", 0) or 0)
    end = start + 3 * H1_MINUTES

    window = [bar for bar in m15_bars
              if start <= int(bar.get("utcTimestampInMinutes", 0) or 0) < end]

    gate = ImbalanceGate(period=M15_MINUTES)
    level_key, direction, note = NESTED_ALERT_SPECS[which]
    entries = []
    for i in range(2, len(window)):
        triplet = window[i - 2:i + 1]
        if fresh_imbalance(triplet, M15_MINUTES) != which:
            continue
        bottom, top = gap_bounds(triplet, which)
        if not overlaps(bottom, top, h1_bottom, h1_top):
            continue
        newest_ts = int(triplet[-1].get("utcTimestampInMinutes", 0) or 0)
        if not gate.admit(newest_ts):
            continue
        c1 = triplet[0]
        entries.append(NestedEntry(
            bottom=bottom, top=top,
            level=(candle_high(c1) if level_key == "high1"
                   else candle_low(c1)),
            direction=direction, note=note, newest_ts=newest_ts))
    return entries
