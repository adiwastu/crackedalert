"""Fresh fair-value-gap (imbalance) detection on completed candles.

A fair value gap is a 3-candle pattern: the newest closed candle's
extreme does not overlap the candle two bars back, leaving a price
void (an imbalance) on the way. Detection only needs the last three
completed trendbars:

    bullish: bar3.low  > bar1.high   -> zone (bar1.high, bar3.low)
    bearish: bar3.high < bar1.low    -> zone (bar3.high, bar1.low)

The pattern is "fresh" when it completes on the newest closed candle,
which is exactly what this module checks. Bars must be consecutive
(same timeframe) and newest-last.
"""

from typing import List, Optional, Tuple

from .alerts import CANDLE_ABOVE, CANDLE_BELOW, CROSSING_DOWN, CROSSING_UP

# Trendbar prices are ints scaled by PRICE_SCALE (see ctrader/candles.py).
PRICE_SCALE = 100000

# Auto-alerts created when a fresh imbalance forms. Each entry:
# (kind, candle-1 level to watch, alert direction, note).
# Bullish (1->2->3 up): enter the demand zone at candle1.high, and the
# flip trigger is an H1 close below candle1.low.
# Bearish: enter the supply zone at candle1.low, and the flip trigger is
# an H1 close above candle1.high.
IMBALANCE_ALERT_SPECS: dict = {
    "bullish": (
        ("price", "high1", CROSSING_DOWN, "masuk DH1. WATCH!"),
        ("candle", "low1", CANDLE_BELOW,
         "strike 1 of FLIP to the DOWNSIDE. WATCH!"),
    ),
    "bearish": (
        ("price", "low1", CROSSING_UP, "masuk S H1. WATCH!"),
        ("candle", "high1", CANDLE_ABOVE,
         "strike 1 of FLIP to the UPSIDE. WATCH!"),
    ),
}

# An M15 imbalance nested in a live H1 zone arms one alert: entry at its
# own candle 1, the same way round as the H1 entry. No flip of its own --
# the H1 zone it sits in already carries one. Each entry:
# (candle-1 level to watch, alert direction, note).
NESTED_ALERT_SPECS: dict = {
    "bullish": ("high1", CROSSING_DOWN, "masuk DM15. WATCH!"),
    "bearish": ("low1", CROSSING_UP, "masuk S M15. WATCH!"),
}


def zone_name(which: str, timeframe: str) -> str:
    """DH1 / S H1, DM15 / S M15: a bullish gap is demand, bearish supply."""
    return ("D%s" if which == "bullish" else "S %s") % timeframe.upper()


def candle_high(bar: dict) -> float:
    """Absolute high of one completed trendbar."""
    return _high(bar)


def candle_low(bar: dict) -> float:
    """Absolute low of one completed trendbar."""
    return _low(bar)


def fresh_imbalance(bars: List[dict], period: int = 60) -> Optional[str]:
    """Return 'bullish' or 'bearish' when the newest of the last three
    completed trendbars completes an FVG, else None.

    period is the timeframe in minutes; the three bars must be exactly
    one period apart. Each bar needs 'utcTimestampInMinutes', 'low' and
    'deltaHigh'.
    """
    if bars is None or len(bars) < 3:
        return None
    c1, _c2, c3 = bars[-3], bars[-2], bars[-1]

    ts1 = int(c1.get("utcTimestampInMinutes", 0) or 0)
    ts3 = int(c3.get("utcTimestampInMinutes", 0) or 0)
    if ts1 <= 0 or ts3 - ts1 != 2 * period:
        return None

    h1 = _high(c1)
    l1 = _low(c1)
    h3 = _high(c3)
    l3 = _low(c3)

    if l3 > h1:
        return "bullish"
    if h3 < l1:
        return "bearish"
    return None


def gap_bounds(bars: List[dict], which: str) -> Tuple[float, float]:
    """(bottom, top) of the gap the newest triplet left, for an FVG
    already known to be `which`: bullish (c1.high, c3.low), bearish
    (c3.high, c1.low)."""
    c1, c3 = bars[-3], bars[-1]
    if which == "bullish":
        return _high(c1), _low(c3)
    return _high(c3), _low(c1)


def overlaps(bottom: float, top: float,
             other_bottom: float, other_top: float) -> bool:
    """True when two price ranges share any interior: one inside the
    other, crossing an edge, or slicing through. Strict, like every other
    comparison here -- ranges that only touch at an edge do not overlap."""
    return bottom < other_top and top > other_bottom


def just_closed_ts(now: float, period: int) -> int:
    """UTC-minute open of the bar that most recently closed, at `now`
    (unix seconds). A check running just after a boundary should find
    this bar as its newest; if it does not, the gateway has not published
    it yet."""
    return int(now // (period * 60)) * period - period


class ImbalanceGate:
    """Decides whether a fresh imbalance should create alerts.

    Consecutive triplets overlap by two candles, so one impulse prints a
    fresh imbalance on each of its candles. Only the first earns alerts.

    "First" has to mean the first this gate saw, not the first the
    market printed. Reading it off the bars alone -- skip if the previous
    candle also completed one -- assumed every hourly check had run on
    fresh data. When the check for an impulse's first candle missed
    (stale gateway data, a restart, a skipped hour), that earlier
    imbalance was never evaluated, yet the bars still showed it, so the
    filter skipped the only one that was: the whole impulse produced
    nothing.

    Held in memory. A restart forgets the last imbalance, so a
    continuation straight after a restart alerts again. That is the safe
    way to be wrong -- one duplicate rather than a silent miss.
    """

    def __init__(self, period: int = 60) -> None:
        self._period = period
        self._last_seen: Optional[int] = None

    @property
    def last_seen(self) -> Optional[int]:
        return self._last_seen

    def admit(self, newest_ts: int) -> bool:
        """Record an imbalance completing on the bar opening at newest_ts
        (UTC minutes) and say whether it should alert."""
        previous, self._last_seen = self._last_seen, newest_ts
        if previous is None:
            return True
        if newest_ts == previous:
            return False              # the same imbalance evaluated again
        return newest_ts - previous != self._period    # continues one we saw


def back_to_back(bars: List[dict], period: int = 60) -> bool:
    """True when the candle before the newest one also completed an FVG.

    A fact about the market, for display (/imbalance). Do not use it to
    decide whether to alert: the market having an imbalance on the
    previous candle does not mean the bot alerted it. That is
    ImbalanceGate's job.

    Direction is not considered. Needs four bars to answer; with fewer
    it returns False.
    """
    if bars is None or len(bars) < 4:
        return False
    return fresh_imbalance(bars[:-1], period) is not None


def _low(bar: dict) -> float:
    return int(bar.get("low", 0) or 0) / PRICE_SCALE


def _high(bar: dict) -> float:
    low = int(bar.get("low", 0) or 0)
    delta_high = int(bar.get("deltaHigh", 0) or 0)
    return (low + delta_high) / PRICE_SCALE
